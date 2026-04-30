CC      = gcc
CFLAGS  = -shared -fPIC -Wall -O2
INCLUDES = -I/usr/include
TARGET  = spank_vmocs.so
SRC     = spank_vmocs.c

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) $(INCLUDES) -o $(TARGET) $(SRC)

install: $(TARGET)
	install -m 755 $(TARGET) /usr/lib64/slurm/

clean:
	rm -f $(TARGET)
