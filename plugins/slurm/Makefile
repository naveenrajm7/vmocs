prefix      ?= /usr/local
libdir      ?= $(prefix)/lib
datarootdir ?= $(prefix)/share
datadir     ?= $(datarootdir)

PLUGINDIR := $(abspath $(DESTDIR)/$(libdir)/slurm)
CONFDIR   := $(abspath $(DESTDIR)/$(datadir)/vmocs)

# Keep in sync with debian/changelog when releasing.
VMOCS_VER ?= 0.0.4

PKG    := vmocs-slurm-plugin
PLUGIN := spank_vmocs.so
CONF   := vmocs.conf
SRCS   := spank_vmocs.c
OBJS   := $(SRCS:.c=.o)
DEPS   := $(OBJS:.o=.d)

CPPFLAGS := -D_GNU_SOURCE $(CPPFLAGS)
CFLAGS   := -std=gnu11 -O2 -Wall -fstack-protector-strong -fPIC $(CFLAGS)
LDFLAGS  := -Wl,-znoexecstack -Wl,-zrelro -Wl,-znow $(LDFLAGS)

.PHONY: all install uninstall clean deb rpm

all: $(PLUGIN)

$(OBJS): %.o: %.c
	$(CC) $(CFLAGS) $(CPPFLAGS) -MMD -MF $*.d -c $<

$(PLUGIN): $(OBJS)
	$(CC) -shared $(LDFLAGS) -o $@ $^
	strip --strip-unneeded -R .comment $@

# The plugstack fragment carries the absolute path to the plugin so that it
# resolves regardless of where the distro puts Slurm's plugin directory.
install: all
	install -d -m 755 $(PLUGINDIR)
	install -m 644 $(PLUGIN) $(PLUGINDIR)
	install -d -m 755 $(CONFDIR)
	printf '%s\n' \
	    '# vmocs SPANK plugin. Enable it by adding the following line to' \
	    '# /etc/slurm/plugstack.conf:' \
	    '#     include $(datadir)/vmocs/$(CONF)' \
	    '#' \
	    '# Optional plugin arguments, appended to the line below:' \
	    '#     vmocs_path=PREFIX   prefix containing bin/vmocs (default: use PATH)' \
	    '#     vmocs_conf=PATH     vmocs.yaml passed as --config to every vmocs call' \
	    'optional $(libdir)/slurm/$(PLUGIN)' \
	    | install -m 644 /dev/stdin $(CONFDIR)/$(CONF)

uninstall:
	$(RM) $(PLUGINDIR)/$(PLUGIN)
	$(RM) $(CONFDIR)/$(CONF)

clean:
	$(RM) $(OBJS) $(DEPS) $(PLUGIN)

deb: clean
	dpkg-buildpackage -us -uc -b

rpm: clean
	mkdir -p rpm/SOURCES
	tar -caf rpm/SOURCES/$(PKG)-$(VMOCS_VER).tar.xz \
	    --transform='s,^\.,$(PKG)-$(VMOCS_VER),' \
	    --transform='s,^LICENSE$$,$(PKG)-$(VMOCS_VER)/LICENSE,' \
	    --exclude=rpm --exclude=debian . -C $(CURDIR)/../.. LICENSE
	rpmbuild --clean -ba -D"_topdir $(CURDIR)/rpm" -D"VMOCS_VER $(VMOCS_VER)" $(PKG).spec
	$(RM) -r $(addprefix rpm/, BUILDROOT BUILD SOURCES)

-include $(DEPS)
