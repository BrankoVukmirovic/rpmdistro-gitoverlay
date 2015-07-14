#!/usr/bin/python -tt
# by skvidal@fedoraproject.org
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Library General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301  USA.
# copyright 2012 Red Hat, Inc.

# SUMMARY
# mockchain
# take a mock config and a series of srpms
# rebuild them one at a time
# adding each to a local repo
# so they are available as build deps to next pkg being built
from __future__ import print_function
try:
    from six.moves.urllib_parse import urlsplit  # pylint: disable=no-name-in-module
except ImportError:
    from urlparse import urlsplit
import sys
import subprocess
import os
import optparse
import tempfile
import shutil
import time
import re

import mockbuild.util

# all of the variables below are substituted by the build system
__VERSION__ = "unreleased_version"
SYSCONFDIR = os.path.join(os.path.dirname(os.path.realpath(sys.argv[0])), "..", "etc")
PYTHONDIR = os.path.dirname(os.path.realpath(sys.argv[0]))
PKGPYTHONDIR = os.path.join(os.path.dirname(os.path.realpath(sys.argv[0])), "mockbuild")
MOCKCONFDIR = os.path.join(SYSCONFDIR, "mock")
# end build system subs

mockconfig_path = '/etc/mock'

def createrepo(path):
    if os.path.exists(path + '/repodata/repomd.xml'):
        comm = ['/usr/bin/createrepo_c', '--update', path]
    else:
        comm = ['/usr/bin/createrepo_c', path]
    cmd = subprocess.Popen(comm,
             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = cmd.communicate()
    return out, err

def parse_args(args):
    parser = optparse.OptionParser('\nmockchain -r mockcfg pkg1 [pkg2] [pkg3]')
    parser.add_option('-r', '--root', default=None, dest='chroot',
            help="chroot config name/base to use in the mock build")
    parser.add_option('-l', '--localrepo', default=None,
            help="local path for the local repo, defaults to making its own")
    parser.add_option('-c', '--continue', default=False, action='store_true',
            dest='cont',
            help="if a pkg fails to build, continue to the next one")
    parser.add_option('-a', '--addrepo', default=[], action='append',
            dest='repos',
            help="add these repo baseurls to the chroot's yum config")
    parser.add_option('--recurse', default=False, action='store_true',
            help="if more than one pkg and it fails to build, try to build the rest and come back to it")
    parser.add_option('--logdir', default=None, dest='logdir',
            help="Add log files to this directory")
    parser.add_option('--tmp_prefix', default=None, dest='tmp_prefix',
            help="tmp dir prefix - will default to username-pid if not specified")
    parser.add_option('-m', '--mock-option', default=[], action='append',
            dest='mock_option',
            help="option to pass directly to mock")


    opts, args = parser.parse_args(args)
    if opts.recurse:
        opts.cont = True

    if not opts.chroot:
        print("You must provide an argument to -r for the mock chroot")
        sys.exit(1)


    if len(args) < 2:
        print("You must specifiy at least 1 package to build")
        sys.exit(1)


    return opts, args

REPOS_ID = []
def generate_repo_id(baseurl):
    """ generate repository id for yum.conf out of baseurl """
    repoid = "/".join(baseurl.split('//')[1:]).replace('/', '_')
    repoid = re.sub(r'[^a-zA-Z0-9_]', '', repoid)
    suffix = ''
    i = 1
    while repoid + suffix in REPOS_ID:
        suffix = str(i)
        i += 1
    repoid = repoid + suffix
    REPOS_ID.append(repoid)
    return repoid

def add_local_repo(infile, destfile, baseurl, repoid=None):
    """take a mock chroot config and add a repo to it's yum.conf
       infile = mock chroot config file
       destfile = where to save out the result
       baseurl = baseurl of repo you wish to add"""
    global config_opts

    try:
        # What's going on here is we're dynamically executing the config
        # file as code, resetting any previous modifications to the `config_opts`
        # variable.
        with open(infile) as f:
            code = compile(f.read(), infile, 'exec')
        exec(code)

        # Add overrides to the default mock config here:
        # Ensure we're using the priorities plugin
        config_opts['priorities.conf'] = '\n[main]\nenabled=1\n'
        config_opts['yum.conf'] = config_opts['yum.conf'].replace('[main]\n', '[main]\nplugins=1\n')

        if not repoid:
            repoid = generate_repo_id(baseurl)
        else:
            REPOS_ID.append(repoid)
        localyumrepo = """
[%s]
name=%s
baseurl=%s
enabled=1
skip_if_unavailable=1
metadata_expire=30
cost=1
priority=1
""" % (repoid, baseurl, baseurl)

        config_opts['yum.conf'] += localyumrepo
        br_dest = open(destfile, 'w')
        for k, v in list(config_opts.items()):
            br_dest.write("config_opts[%r] = %r\n" % (k, v))
        br_dest.close()
        return True, ''
    except (IOError, OSError):
        return False, "Could not write mock config to %s" % destfile

    return True, ''

def do_build(opts, cfg, pkg):

    # returns 0, cmd, out, err = failure
    # returns 1, cmd, out, err  = success
    # returns 2, None, None, None = already built

    s_pkg = os.path.basename(pkg)
    pdn = s_pkg.replace('.temp.src.rpm', '')
    resdir = '%s/%s' % (opts.local_repo_dir, pdn)
    resdir = os.path.normpath(resdir)
    if not os.path.exists(resdir):
        os.makedirs(resdir)

    success_file = resdir + '/success'
    fail_file = resdir + '/fail'

    if os.path.exists(success_file):
        return 2, None, None, None

    # clean it up if we're starting over :)
    if os.path.exists(fail_file):
        os.unlink(fail_file)

    mockcmd = ['/usr/bin/mock',
               '--nocheck',   # Tests should run after builds
               '--yum',
               '--configdir', opts.config_path,
               '--resultdir', resdir,
               '--uniqueext', opts.uniqueext,
               '-r', cfg, ]
    # heuristic here, if user pass for mock "-d foo", but we must be care to leave
    # "-d'foo bar'" or "--define='foo bar'" as is
    compiled_re_1 = re.compile(r'^(-\S)\s+(.+)')
    compiled_re_2 = re.compile(r'^(--[^ =])[ =](\.+)')
    for option in opts.mock_option:
        r_match = compiled_re_1.match(option)
        if r_match:
            mockcmd.extend([r_match.group(1), r_match.group(2)])
        else:
            r_match = compiled_re_2.match(option)
            if r_match:
                mockcmd.extend([r_match.group(1), r_match.group(2)])
            else:
                mockcmd.append(option)

    mockcmd.append(pkg)
    print('Executing: {0}'.format(subprocess.list2cmdline(mockcmd)))
    cmd = subprocess.Popen(mockcmd,
           stdout=subprocess.PIPE,
           stderr=subprocess.PIPE)
    out, err = cmd.communicate()
    if cmd.returncode == 0:
        open(success_file, 'w').write('done\n')
        ret = 1
    else:
        sys.stderr.write(err)
        open(fail_file, 'w').write('undone\n')
        ret = 0

    return ret, cmd, out, err


def log(lf, msg):
    if lf:
        now = time.time()
        try:
            open(lf, 'a').write(str(now) + ':' + msg + '\n')
        except (IOError, OSError) as e:
            print('Could not write to logfile %s - %s' % (lf, str(e)))
    print(msg)


config_opts = {}

def main(args):
    print ("args: {0}".format(args))
    opts, args = parse_args(args)
    print ("opts: {0} args: {1}".format(opts, args))
    # take mock config + list of pkgs
    cfg = opts.chroot
    pkgs = args[1:]

    global config_opts
    config_opts = mockbuild.util.load_config(mockconfig_path, cfg, None, __VERSION__, PKGPYTHONDIR)

    if not opts.tmp_prefix:
        try:
            opts.tmp_prefix = os.getlogin()
        except OSError as e:
            print("Could not find login name for tmp dir prefix add --tmp_prefix")
            sys.exit(1)
    pid = os.getpid()
    opts.uniqueext = '%s-%s' % (opts.tmp_prefix, pid)

    if opts.logdir:
        opts.logfile = os.path.join(opts.logdir, 'mockchain.log')
        if os.path.exists(opts.logfile):
            os.unlink(opts.logfile)
        log(opts.logfile, "starting logfile: %s" % opts.logfile)
    else:
        opts.logfile = None

    local_tmp_dir = tempfile.mkdtemp('mockchain')

    opts.local_repo_dir = opts.localrepo

    if not os.path.exists(opts.local_repo_dir):
        os.makedirs(opts.local_repo_dir, mode=0o755)

    local_baseurl = "file://%s" % opts.local_repo_dir
    log(opts.logfile, "results dir: %s" % opts.local_repo_dir)
    opts.config_path = os.path.normpath(local_tmp_dir + '/configs/' + config_opts['chroot_name'] + '/')

    if not os.path.exists(opts.config_path):
        os.makedirs(opts.config_path, mode=0o755)

    log(opts.logfile, "config dir: %s" % opts.config_path)

    my_mock_config = os.path.join(opts.config_path, "{0}.cfg".format(config_opts['chroot_name']))

    # modify with localrepo
    res, msg = add_local_repo(config_opts['config_file'], my_mock_config, local_baseurl, 'local_build_repo')
    if not res:
        log(opts.logfile, "Error: Could not write out local config: %s" % msg)
        sys.exit(1)

    for baseurl in opts.repos:
        res, msg = add_local_repo(my_mock_config, my_mock_config, baseurl)
        if not res:
            log(opts.logfile, "Error: Could not add: %s to yum config in mock chroot: %s" % (baseurl, msg))
            sys.exit(1)


    # these files needed from the mock.config dir to make mock run
    for fn in ['site-defaults.cfg', 'logging.ini']:
        pth = mockconfig_path + '/' + fn
        shutil.copyfile(pth, opts.config_path + '/' + fn)


    # createrepo on it
    out, err = createrepo(opts.local_repo_dir)
    if err.strip():
        log(opts.logfile, "Error making local repo: %s" % opts.local_repo_dir)
        log(opts.logfile, "Err: %s" % err)
        sys.exit(1)


    downloaded_pkgs = {}
    built_pkgs = []
    try_again = True
    to_be_built = pkgs
    return_code = 0
    num_of_tries = 0
    while try_again:
        num_of_tries += 1
        failed = []
        for pkg in to_be_built:
            if not pkg.endswith('.rpm'):
                log(opts.logfile, "%s doesn't appear to be an rpm - skipping" % pkg)
                failed.append(pkg)
                continue

            log(opts.logfile, "Start build: %s" % pkg)
            ret, cmd, out, err = do_build(opts, config_opts['chroot_name'], pkg)
            log(opts.logfile, "End build: %s" % pkg)
            if ret == 0:
                failed.append(pkg)
                log(opts.logfile, "Error building %s." % os.path.basename(pkg))
                if opts.recurse:
                    log(opts.logfile, "Will try to build again (if some other package will succeed).")
                else:
                    log(opts.logfile, "See logs/results in %s" % opts.local_repo_dir)
            elif ret == 1:
                log(opts.logfile, "Success building %s" % os.path.basename(pkg))
                built_pkgs.append(pkg)
                # createrepo with the new pkgs
                out, err = createrepo(opts.local_repo_dir)
                if err.strip():
                    log(opts.logfile, "Error making local repo: %s" % opts.local_repo_dir)
                    log(opts.logfile, "Err: %s" % err)
            elif ret == 2:
                log(opts.logfile, "Skipping already built pkg %s" % os.path.basename(pkg))

        if failed and opts.recurse:
            if len(failed) != len(to_be_built):
                to_be_built = failed
                try_again = True
                log(opts.logfile, 'Some package succeeded, some failed.')
                log(opts.logfile, 'Trying to rebuild %s failed pkgs, because --recurse is set.' % len(failed))
            else:
                log(opts.logfile, "Tried %s times - following pkgs could not be successfully built:" % num_of_tries)
                for pkg in failed:
                    msg = pkg
                    if pkg in downloaded_pkgs:
                        msg = downloaded_pkgs[pkg]
                    log(opts.logfile, msg)
                try_again = False
                return_code = 2
        else:
            try_again = False
            if failed:
                return_code = 2

    log(opts.logfile, "Results out to: %s" % opts.local_repo_dir)
    log(opts.logfile, "Pkgs built: %s" % len(built_pkgs))
    if built_pkgs:
        if failed:
            if len(built_pkgs):
                log(opts.logfile, "Some packages successfully built in this order:")
        else:
            log(opts.logfile, "Packages successfully built in this order:")
        for pkg in built_pkgs:
            log(opts.logfile, pkg)
    return return_code

if __name__ == "__main__":
    sys.exit(main(sys.argv))
