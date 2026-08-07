# Tags the build with the Slurm release it was compiled against, e.g. ".sl2411".
# Expands to nothing when Slurm was not installed from an RPM.
%global slurm_version %(rpm -q --qf '%%{VERSION}' slurm-devel 2>/dev/null | awk -F. '/^[0-9]/{print ".sl"$1$2}')
%define _use_internal_dependency_generator 0
%define __find_requires %{_builddir}/find-requires

Name:           vmocs-slurm-plugin
Version:        %{VMOCS_VER}
Release:        1%{slurm_version}%{?dist}
Summary:        SPANK plugin that boots vmocs virtual machines as Slurm jobs
License:        GPL-3.0-or-later
URL:            https://github.com/naveenrajm7/vmocs
Source:         %{name}-%{version}.tar.xz

BuildRequires:  make gcc slurm-devel

%global debug_package %{nil}

%description
A SPANK plugin for the Slurm Workload Manager that lets users boot a virtual
machine as their job with "srun --vm-image TEMPLATE". The plugin maps the
Slurm CPU, memory and GPU allocation onto the guest, keeps the job alive for
the VM's lifetime, and tears the VM down when the job ends.

The vmocs command line tool must be installed separately on every compute
node; it is not shipped as an RPM.

%prep
%setup -q
# Dummy binary used only to derive an RPM dependency on libslurm.so, pinning
# the package to the Slurm release its SPANK ABI was built against.
echo 'int main(){}' > %{_builddir}/libslurm_dummy.c
cat <<EOF > %{_builddir}/find-requires
#!/bin/sh
{ echo %{_builddir}/libslurm_dummy; cat; } | \
    %{_rpmconfigdir}/find-requires
EOF
chmod +x %{_builddir}/find-requires

%build
%make_build prefix=%{_prefix}
# --no-as-needed is required: the dummy references no libslurm symbols, so
# toolchains that default to --as-needed would drop the DT_NEEDED entry.
%{__cc} -Wl,--no-as-needed -o %{_builddir}/libslurm_dummy %{_builddir}/libslurm_dummy.c -lslurm

%install
%make_install prefix=%{_prefix} libdir=%{_libdir} datarootdir=%{_datadir} DESTDIR=%{buildroot}

%files
%license LICENSE
%doc README.md
%{_libdir}/slurm/spank_vmocs.so
%dir %{_datadir}/vmocs
%{_datadir}/vmocs/vmocs.conf

%changelog
* Fri Aug 07 2026 Naveenraj Muthuraj <22456988+naveenrajm7@users.noreply.github.com> - 0.0.4-1
- Initial package
