# SPDX-License-Identifier: MIT
#
# Fedora RPM spec for the sarvam-meter Plasma applet and its local daemon.
#
# The source tarball is produced by CI (and by hand) from a git checkout:
#
#   NAME=plasma-applet-sarvam-meter
#   VERSION=0.1.0
#   git archive --format=tar.gz --prefix="${NAME}-${VERSION}/" \
#       -o "${NAME}-${VERSION}.tar.gz" HEAD
#   rpmbuild -ba packaging/plasma-applet-sarvam-meter.spec \
#       --define "_topdir $PWD/rpmbuild" \
#       --define "_sourcedir $PWD/rpmbuild/SOURCES" \
#       --define "sarvam_release 1.<utc-timestamp>.git<short-sha>"
#
# The tarball's single top-level directory is %{name}-%{version}/, which is
# exactly what the %setup below expects.
#
# This spec deliberately declares *no* BuildRequires: the package is pure
# Python source plus data files, and it must build in a bare Fedora container
# that only has rpm-build installed.

Name:           plasma-applet-sarvam-meter
Version:        0.1.0
# The Release must change whenever the contents do, or `dnf`/`rpm -U` see an
# identical NEVRA and refuse to replace an installed copy -- which is how a
# rebuilt RPM can be "reinstalled" while every file on disk stays old.  CI passes
# --define "sarvam_release 1.<timestamp>.git<sha>"; a hand build gets 1.
%{!?sarvam_release:%global sarvam_release 1}
Release:        %{sarvam_release}%{?dist}
Summary:        KDE Plasma 6 applet and local daemon that meter Sarvam AI credit usage

License:        MIT
URL:            https://github.com/creepy-pasta101/sarvam-meter-plasmoid
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

Requires:       python3 >= 3.9
Requires:       plasma-workspace >= 6.0
# Declared because the QML imports them directly. plasma-workspace pulls these in
# transitively today, but a direct import should not depend on someone else's
# dependency graph -- plasma5support in particular is a recent split-out, and a
# missing module means the applet fails to load with "module not installed".
Requires:       plasma5support
Requires:       kf6-kirigami
Requires:       kf6-kcmutils
Requires:       systemd
# Optional: the settings page can sign in to the Sarvam dashboard in a webview.
# Without it the page falls back to the CLI, which is why this is not a hard
# dependency.
Recommends:     qt6-qtwebengine
%{?systemd_requires}

# --- Minimal build-host fallbacks -------------------------------------------
# Because there are no BuildRequires, the macros normally supplied by
# python-rpm-macros and systemd-rpm-macros may be absent on the build host.
# These definitions are used only when the macro packages are missing, and
# they resolve to the same values those packages provide on Fedora.
%{!?python3_sitelib:%global python3_sitelib %(python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))')}
%{!?_userunitdir:%global _userunitdir %{_prefix}/lib/systemd/user}
%{!?systemd_user_post:%global systemd_user_post() %%{nil}}
%{!?systemd_user_postun:%global systemd_user_postun() %%{nil}}

%description
sarvam-meter is a KDE Plasma 6 applet (widget) plus a small local Python
daemon that reports Sarvam AI credit usage and CLIProxyAPI OAuth quota.

Sarvam AI publishes no usage or credit-balance REST endpoint, so the package
scrapes Sarvam's own dashboard with a session the user captures once, and
reports the authoritative spend and the real credit balance from it. A local
SQLite ledger under ~/.local/share/sarvam-meter/ holds entries recorded by hand
with `sarvam-meter record`. The applet reads the resulting status JSON from the
daemon over its local socket and performs no network access of its own.

Nothing in this package relays API traffic: it is not a proxy and it opens no
TCP port for one.

The package also ships a systemd *user* unit for the daemon and an example
configuration file.

%prep
%setup -q

%build
# Pure Python source and data files: there is nothing to build.

%install
install -d -m 0755 %{buildroot}%{python3_sitelib}/sarvam_meter
install -m 0644 sarvam_meter/*.py %{buildroot}%{python3_sitelib}/sarvam_meter/

install -d -m 0755 %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/sarvam-meter <<'SH'
#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Console entry point for the sarvam-meter daemon and CLI."""
import sys

from sarvam_meter.cli import main

if __name__ == "__main__":
    sys.exit(main())
SH
chmod 0755 %{buildroot}%{_bindir}/sarvam-meter

install -d -m 0755 %{buildroot}%{_datadir}/plasma/plasmoids/io.github.creepy_pasta101.sarvammeter
cp -a plasmoid/io.github.creepy_pasta101.sarvammeter/. \
    %{buildroot}%{_datadir}/plasma/plasmoids/io.github.creepy_pasta101.sarvammeter/

install -d -m 0755 %{buildroot}%{_userunitdir}
install -m 0644 packaging/sarvam-meter.service %{buildroot}%{_userunitdir}/sarvam-meter.service

install -d -m 0755 %{buildroot}%{_docdir}/%{name}
install -m 0644 packaging/config.example.json %{buildroot}%{_docdir}/%{name}/config.example.json

%check
python3 -m unittest discover -s tests -v

%post
%systemd_user_post %{name}.service
# A user unit is never enabled by a package: it would start a daemon for every
# account on the machine.  Say exactly what to run instead.
cat <<'EOM'
sarvam-meter: the daemon ships as a systemd user unit. To start it for yourself:
  systemctl --user daemon-reload
  systemctl --user enable --now sarvam-meter.service
  journalctl --user -u sarvam-meter.service -f
To keep it running while logged out: loginctl enable-linger "$USER"
Then capture a dashboard session (Settings > Configure Sarvam Meter, or
  sarvam-meter session set --cookie-file PATH) so the balance can be shown.
EOM

%postun
%systemd_user_postun %{name}.service

%files
%license LICENSE
%doc README.md
%doc docs/*.md
%config(noreplace) %{_docdir}/%{name}/config.example.json
%{python3_sitelib}/sarvam_meter/
%{_bindir}/sarvam-meter
%{_datadir}/plasma/plasmoids/io.github.creepy_pasta101.sarvammeter/
%{_userunitdir}/sarvam-meter.service

%changelog
* Sun Nov 01 2026 Suhan G Paradkar <suhangp2002@gmail.com> - 0.1.0-1
- Initial package: Plasma 6 applet, local metering daemon, user unit.
