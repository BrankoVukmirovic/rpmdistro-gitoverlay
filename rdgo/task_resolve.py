#!/usr/bin/env python
#
# Copyright (C) 2015 Colin Walters <walters@verbum.org>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place - Suite 330,
# Boston, MA 02111-1307, USA.

import os
import json
import argparse
import subprocess
import yaml
import copy

from .utils import log, fatal
from .task import Task
from .git import GitMirror

def require_key(conf, key):
    try:
        return conf[key]
    except KeyError, e:
        fatal("Missing config key {0}".format(key))

class TaskResolve(Task):
    def _url_to_projname(self, url):
        rcolon = url.rfind(':')
        rslash = url.rfind('/')
        basename = url[max(rcolon, rslash)+1:]
        if basename.endswith('.git'):
            return basename[0:-4]
        return basename

    def _expand_srckey(self, component, key):
        val = component[key]
        aliases = self._overlay.get('aliases', [])
        for alias in aliases:
            name = alias['name']
            namec = name + ':'
            if not val.startswith(namec):
                continue
            return alias['url'] + val[len(namec):]
        return val

    def _ensure_key_or(self, dictval, key, value):
        v = dictval.get(key)
        if v is not None:
            return v
        dictval[key] = value
        return value

    def _one_of_keys(self, dictval, first, *args):
        v = dictval.get(first)
        if v is not None:
            return v
        for k in args:
            v = dictval.get(k)
            if v is not None:
                return v
        return None

    def _expand_component(self, component):
        # 'src' and 'distgit' mappings
        if component.get('src') is None:
            fatal("Component {0} is missing 'src' or 'distgit'")

        component['src'] = self._expand_srckey(component, 'src')

        # TODO support pulling VCS from distgit
        
        name = self._ensure_key_or(component, 'name', self._url_to_projname(component['src']))
        pkgname_default = name

        # tag/branch defaults
        if component.get('tag') is None:
            component['branch'] = component.get('branch', 'master')

        spec = component.get('spec')
        if spec is not None:
            if spec == 'internal':
                pass
            else:
                raise ValueError('Unknown spec type {0}'.format(spec))
        else:
            distgit = self._ensure_key_or(component, 'distgit', {})
            pkgname_default = self._ensure_key_or(distgit, 'name', pkgname_default)
            distgit_src = self._ensure_key_or(distgit, 'src', 
                                              self._distgit_prefix + ':' + distgit['name'])
            distgit['src'] = self._expand_srckey(distgit, 'src')

            if distgit.get('tag') is None:
                distgit['branch'] = distgit.get('branch', 'master')

        self._ensure_key_or(component, 'pkgname', pkgname_default)

    def run(self, argv):
        parser = argparse.ArgumentParser(description="Create snapshot.json")
        parser.add_argument('--fetch-all', action='store_true', help='Fetch all git repositories')
        parser.add_argument('-f', '--fetch', action='append', default=[],
                            help='Fetch the specified git repository')
        parser.add_argument('--touch-if-changed', action='store', default=None,
                            help='Create or update timestamp on target path if a change occurred')

        opts = parser.parse_args(argv)

        srcdir = self.workdir + '/src'
        if not os.path.isdir(srcdir):
            fatal("Missing src/ directory; run 'rpmdistro-gitoverlay init'?")

        ovlpath = self.workdir + '/overlay.yml'
        with open(ovlpath) as f:
            self._overlay = yaml.load(f)
            
        self._distgit = require_key(self._overlay, 'distgit')
        self._distgit_prefix = require_key(self._distgit, 'prefix')

        mirror = GitMirror(srcdir)
        expanded = copy.deepcopy(self._overlay)
        for component in expanded['components']:
            self._expand_component(component)
            ref = self._one_of_keys(component, 'freeze', 'branch', 'tag')
            do_fetch = opts.fetch_all or (component['name'] in opts.fetch)
            revision = mirror.mirror(component['src'], ref, fetch=do_fetch)
            component['revision'] = revision

            distgit = component.get('distgit')
            if distgit is not None:
                ref = self._one_of_keys(distgit, 'freeze', 'branch', 'tag')
                do_fetch = opts.fetch_all or (distgit['name'] in opts.fetch)
                revision = mirror.mirror(distgit['src'], ref, fetch=do_fetch)
                distgit['revision'] = revision

        del expanded['aliases']

        expanded['00comment'] = 'Generated by rpmdistro-gitoverlay from overlay.yml: DO NOT EDIT!'

        snapshot_path = self.workdir + '/snapshot.json'
        snapshot_tmppath = snapshot_path + '.tmp'
        with open(snapshot_tmppath, 'w') as f:
            json.dump(expanded, f, indent=4, sort_keys=True)

        changed = True
        if (os.path.exists(snapshot_path) and
            subprocess.call(['cmp', '-s', snapshot_path, snapshot_tmppath]) == 0):
            changed = False
        if changed:
            os.rename(snapshot_tmppath, snapshot_path)
            log("Wrote: " + snapshot_path)
            if opts.touch_if_changed:
                with open(opts.touch_if_changed, 'a'):
                    log("Updated timestamp of {}".format(opts.touch_if_changed))
                    os.utime(opts.touch_if_changed, None)
        else:
            os.unlink(snapshot_tmppath)
            log("No changes.")
                
