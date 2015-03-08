Summary: Overlay repository manager
Name: rpmdistro-gitoverlay
Version: 98e6388
Release: 1%{?dist}
#VCS: https://github.com/cgwalters/rpmdistro-gitoverlay
Source0: %{name}-%{version}.tar.xz
License: LGPLv2+
URL: https://github.com/cgwalters/rpmdistro-gitoverlay
# We always run autogen.sh
BuildRequires: autoconf automake libtool

Requires: python
Requires: pygobject2

%description
Manage an overlay repository from upstream git.

%prep
%autosetup -Sgit

%build
env NOCONFIGURE=1 ./autogen.sh
%configure --disable-silent-rules
make %{?_smp_mflags}

%install
make install DESTDIR=%{buildroot} INSTALL="install -p -c"

%files
%doc COPYING README.md src/py/config.ini.sample
%{_bindir}/%{name}
%{_libdir}/%{name}/
%{_datadir}/%{name}/
