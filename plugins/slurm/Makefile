CC      = gcc
CFLAGS  = -shared -fPIC -Wall -O2
INCLUDES = -I/usr/include
TARGET  = spank_vmocs.so
SRC     = spank_vmocs.c

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) $(INCLUDES) -o $(TARGET) $(SRC)

# Default install path matches this cluster (/etc/slurm/). Override:
#   make install SLURM_PLUGIN_DIR=/usr/lib64/slurm
SLURM_PLUGIN_DIR ?= /etc/slurm

install: $(TARGET)
	install -m 755 $(TARGET) $(SLURM_PLUGIN_DIR)/

clean:
	rm -f $(TARGET)
