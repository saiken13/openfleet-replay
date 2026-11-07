# Cross-compile for x86_64 Linux with Debian/Ubuntu's g++-x86-64-linux-gnu.
# Binaries run on the build machine through qemu-user (CMAKE_CROSSCOMPILING_EMULATOR), so
# ctest executes the real x86_64 code: emulated, not native, and labelled as such.
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR x86_64)
set(CMAKE_C_COMPILER x86_64-linux-gnu-gcc)
set(CMAKE_CXX_COMPILER x86_64-linux-gnu-g++)
set(CMAKE_FIND_ROOT_PATH /usr/x86_64-linux-gnu)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
set(CMAKE_CROSSCOMPILING_EMULATOR qemu-x86_64;-L;/usr/x86_64-linux-gnu)
