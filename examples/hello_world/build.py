#!/usr/bin/python3

import hancho

base_config = hancho.config.extend(
  name        = "base_config",
  toolchain   = "x86_64-linux-gnu",
  cpp_std     = "-std=c++20",
  gcc_opt     = "{'-O3' if build_type == 'release' else '-g -O0'} -MMD",
  warnings    = "-Wall -Werror -Wno-unused-variable -Wno-unused-local-typedefs -Wno-unused-but-set-variable",
  build_type  = "debug",
)

compile_cpp = base_config.extend(
  description = "Compiling {file_in} -> {file_out} ({build_type})",
  command     = "{toolchain}-g++ {cpp_std} {gcc_opt} {warnings} {includes} {defines} -c {file_in} -o {file_out}",
  includes    = "-I.",
  defines     = "",
  file_out    = "{swap_ext(file_in, '.o')}",
  depfile     = "{swap_ext(file_in, '.d')}",
)

link_c_bin = base_config.extend(
  description = "Linking {file_out}",
  command     = "{toolchain}-g++ {gcc_opt} {warnings} {join(files_in)} {join(deps)} {sys_libs} -o {file_out}",
  sys_libs    = "",
)

test_o = compile_cpp(
  file_in  = "src/test.cpp",
  file_out = "obj/test.o"
)

main_o = compile_cpp(
  file_in  = "src/main.cpp",
  file_out = "obj/main.o"
)

main = link_c_bin(
  files_in = [test_o, main_o],
  file_out = "bin/main"
)

hancho.build()
