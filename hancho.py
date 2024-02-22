#!/usr/bin/python3

import argparse
import ast
import asyncio
import inspect
import io
import os
import re
import subprocess
import sys
import types
from os import path

this = sys.modules[__name__]
hancho_root = os.getcwd()
hancho_mods  = {}
base_rule = None
config = None
proc_sem = None
node_visit = 0
flags = None
any_failed = False
total_commands = 0

################################################################################

line_dirty = False

def log(*args, sameline = False, **kwargs):
  if this.flags.silent: return

  output = io.StringIO()
  if sameline: kwargs["end"] = ""
  print(*args, file=output, **kwargs)

  output = output.getvalue()
  if not output: return

  if sameline:
    sys.stdout.write("\r")
    sys.stdout.write(output[:os.get_terminal_size().columns - 1])
    sys.stdout.write("\x1B[K")
    this.line_dirty = True
  else:
    if this.line_dirty: sys.stdout.write("\n")
    sys.stdout.write(output)
    this.line_dirty = output[-1] != '\n'

################################################################################

def init():
  this.base_rule = Rule(
    desc      = "{files_in} -> {files_out}",
    build_dir = "build",
    root_dir  = hancho_root,
    quiet     = False, # Don't print this task's output
    force     = False, # Force this task to run
    expand    = expand,
    flatten   = flatten,
    join      = join,
    len       = len,
    run_cmd   = run_cmd,
    swap_ext  = swap_ext
  )

  # Hancho's global configuration object
  this.config = Rule(
    verbose   = False, # Print verbose build info
    quiet     = False, # Don't print any task output
    serial    = False, # Do not parallelize tasks
    dryrun    = False, # Do not actually run tasks
    debug     = False, # Print debugging information
    force     = False, # Force all tasks to run
  )

################################################################################

def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('filename',   default="build.hancho", nargs="?")
  parser.add_argument('--verbose',   default=False, action='store_true', help='Print verbose build info')
  parser.add_argument('--serial',    default=False, action='store_true', help='Do not parallelize commands')
  parser.add_argument('--dryrun',    default=False, action='store_true', help='Do not run commands')
  parser.add_argument('--debug',     default=False, action='store_true', help='Dump debugging information')
  parser.add_argument('--force',     default=False, action='store_true', help='Force rebuild of everything')
  parser.add_argument('--quiet',     default=False, action='store_true', help='Mute command output')
  parser.add_argument('--dump',      default=False, action='store_true', help='Dump debugging info for all tasks')
  parser.add_argument('--multiline', default=False, action='store_true', help='Print multiple lines of output')
  parser.add_argument('--test',      default=False, action='store_true', help='Run .hancho file as a unit test')
  parser.add_argument('--silent',    default=False, action='store_true', help='No output')

  parser.add_argument('-D', action='append', type=str)
  (this.flags, unrecognized) = parser.parse_known_args()

  this.base_rule.quiet = this.flags.quiet
  this.base_rule.force = this.flags.force

  this.config.verbose   = this.flags.verbose   # Print verbose build info
  this.config.quiet     = this.flags.quiet     # Don't print any task output
  this.config.serial    = this.flags.serial    # Do not parallelize tasks
  this.config.dryrun    = this.flags.dryrun    # Do not actually run tasks
  this.config.debug     = this.flags.debug     # Print debugging information
  this.config.force     = this.flags.force     # Force all tasks to run
  this.config.multiline = this.flags.multiline # Print multiple lines of output

  this.proc_sem = asyncio.Semaphore(1 if this.flags.serial else os.cpu_count())

  # A reference to this module is already in sys.modules["__main__"].
  # Stash another reference in sys.modules["hancho"] so that build.hancho and
  # descendants don't try to load a second copy of us.
  sys.modules["hancho"] = this

  build_path = path.join(this.hancho_root, this.flags.filename)
  mod_name = path.split(this.flags.filename)[1].split('.')[0]

  async def start():
    async_mod = async_load_module(mod_name, build_path)
    await asyncio.create_task(async_mod)
    while True:
      pending_tasks = asyncio.all_tasks() - {asyncio.current_task()}
      if not pending_tasks: break
      await asyncio.wait(pending_tasks)

  asyncio.run(start())

  if total_commands == 0:
    log("hancho: no work to do.")

  #log("", end="")
  #print()
  #print()
  if line_dirty: print()

  sys.exit(-1 if this.any_failed else 0)

  #log("[    ] done")

################################################################################

def stack_deps():
  f = inspect.currentframe()
  result = set()
  while f is not None:
    if f.f_code.co_filename.startswith(this.hancho_root):
      result.add(path.abspath(f.f_code.co_filename))
    f = f.f_back
  return list(result)

################################################################################
# Hancho's Rule object behaves like a Javascript object and implements a basic
# form of prototypal inheritance via Rule.base

class Rule(dict):

  # "base" defaulted because base must always be present, otherwise we
  # infinite-recurse.
  def __init__(self, *, base = None, **kwargs):
    self |= kwargs
    self.base = base

  def __missing__(self, key):
    return self.base[key] if self.base else None

  def __setattr__(self, key, value):
    self.__setitem__(key, value)

  def __getattr__(self, key):
    #log(f"key {key}")
    return self.__getitem__(key)

  def __repr__(self):
    return repr_val(self, 0)

  def __call__(self, **kwargs):
    return queue2(self.extend(**kwargs))

  def extend(self, **kwargs):
    return Rule(base = self, **kwargs)

  def expand(self, template):
    return expand(template, self)

################################################################################
# Hancho's module loader. Looks for {mod_dir}.hancho or build.hancho in either
# the calling .hancho file's directory, or relative to hancho_root. Modules
# loaded by this method are _not_ added to sys.modules - they're in
# hancho.hancho_mods

async def load(mod_path):
  old_path = mod_path
  mod_head = path.split(mod_path)[0]
  mod_tail = path.split(mod_path)[1]

  search_paths = []
  search_files = []

  if re.search("\w+\.\w+$", mod_path):
    search_files.append(mod_tail)
    search_paths.append(path.join(os.getcwd(), mod_head))
    search_paths.append(path.join(hancho_root, mod_head))
  else:
    search_files.append(f"{mod_tail}.hancho")
    search_files.append(f"build.hancho")
    search_paths.append(path.join(os.getcwd(), mod_path))
    search_paths.append(path.join(hancho_root, mod_path))

  for mod_file in search_files:
    for mod_path in search_paths:
      abs_path = path.abspath(path.join(mod_path, mod_file))
      if not path.exists(abs_path):
        continue
      if abs_path in hancho_mods:
        return hancho_mods[abs_path]

      print(abs_path)

      mod_name = mod_file.split(".")[0]

      # FIXME we may not always be in the right directory if we're doing async
      # stuff and we load multiple modules...
      old_dir = os.getcwd()
      os.chdir(path.split(abs_path)[0])
      result = await async_load_module(mod_name, abs_path)
      os.chdir(old_dir)

      hancho_mods[abs_path] = result
      return result

  log(f"Could not load module {old_path}")
  sys.exit(-1)

################################################################################
# Python voodoo to manually load a module from a file, compile it with
# PyCF_ALLOW_TOP_LEVEL_AWAIT set, call its code asynchronously, and then return
# a promise for the loaded module.

async def async_load_module(mod_name, mod_path):
  source = open(mod_path, "r").read()
  code = compile(source, mod_path, 'exec', dont_inherit=True, flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
  module = type(sys)(mod_name)
  module.__file__ = mod_path
  import builtins
  module.__builtins__ = builtins
  func = types.FunctionType(code, module.__dict__)()
  if inspect.isawaitable(func): await func
  return module

################################################################################
# Minimal JSON-style pretty printer for Rule, used by --debug

def repr_dict(d, depth):
  result = "{\n"
  for (k,v) in d.items():
    result += "  " * (depth + 1) + repr_val(k, depth + 1) + " : "
    result += repr_val(v, depth + 1) + ",\n"
  result += "  " * depth + "}"
  return result

def repr_list(l, depth):
  if len(l) == 0: return "[]"
  if len(l) == 1: return "[" + repr_val(l[0], depth + 1) + "]"
  result = "[\n"
  for v in l:
    result += "  " * (depth + 1) + repr_val(v, depth + 1) + ",\n"
  result += "  " * depth + "]"
  return result

def repr_val(v, depth):
  if v is None:           return "null"
  if isinstance(v, str):  return '"' + v + '"'
  if isinstance(v, dict): return repr_dict(v, depth)
  if isinstance(v, list): return repr_list(v, depth)
  return str(v)

################################################################################
# A trivial templating system that replaces {foo} with the value of rule.foo
# and keeps going until it can't replace anything.

template_regex = re.compile("{[^}]*}")

def expand_once(template, rule):
  if template is None: return ""
  result = ""
  while s := template_regex.search(template):
    result += template[0:s.start()]
    exp = template[s.start():s.end()]
    try:
      replacement = eval(exp[1:-1], None, rule)
      if replacement is not None: result += str(replacement)
    except Exception as foo:
      log(foo)
      result += exp
    template = template[s.end():]
  result += template
  return result

def expand(template, rule):
  for _ in range(100):
    if config.debug: log(f"expand \"{template}\"")
    new_template = expand_once(template, rule)
    if template == new_template:
      if template_regex.search(template):
        log(f"Expanding '{template[0:20]}' is stuck in a loop")
        sys.exit(-1)
      return template
    template = new_template

  log(f"Expanding '{template[0:20]}...' failed to terminate")
  sys.exit(-1)

################################################################################
# Build rule helper methods

def join(names, divider = ' '):
  return "" if names is None else divider.join(names)

def run_cmd(cmd):
  return subprocess.check_output(cmd, shell=True, text=True).strip()

def swap_ext(name, new_ext):
  return path.splitext(name)[0] + new_ext

################################################################################
# Returns true if any file in files_in is newer than any file in files_out.

def check_mtime(files_in, files_out):
  for file_in in files_in:
    mtime_in = path.getmtime(file_in)
    for file_out in files_out:
      mtime_out = path.getmtime(file_out)
      if mtime_in > mtime_out: return True
  return False

################################################################################
# Checks if a task needs to be re-run, and returns a non-empty reason if so.

def needs_rerun(task):
  files_in  = task.abs_files_in
  files_out = task.abs_files_out

  if not files_in:
    return "Always rebuild a target with no inputs"

  if not files_out:
    return "Always rebuild a target with no outputs"

  # Check for missing outputs.
  for file_out in files_out:
    if not path.exists(file_out):
      return f"Rebuilding {files_out} because some are missing"

  # Check the hancho file(s) that generated the task
  if check_mtime(task.meta_deps, files_out):
    return f"Rebuilding {files_out} because its .hancho files have changed"

  # Check user-specified deps.
  if check_mtime(task.deps, files_out):
    return f"Rebuilding {files_out} because a manual dependency has changed"

  # Check GCC-format depfile, if present.
  if task.depfile:
    depfile_name = expand(task.depfile, task)
    if path.exists(depfile_name):
      deplines = open(depfile_name).read().split()
      deplines = [d for d in deplines[1:] if d != '\\']
      if check_mtime(deplines, files_out):
        return f"Rebuilding {files_out} because a dependency in {depfile_name} has changed"

  # Check input files.
  if check_mtime(files_in, files_out):
    return f"Rebuilding {files_out} because an input has changed"

  # All checks passed, so we don't need to rebuild this output.
  if config.debug: log(f"Files {files_out} are up to date")

  # All deps were up-to-date, nothing to do.
  return None

################################################################################
# Slightly weird method that flattens out an arbitrarily-nested list of strings
# and promises-for-strings into a flat array of actual strings.

async def flatten(x):
  if x is None: return []
  if inspect.iscoroutine(x):
    log("Can't flatten a raw coroutine!")
    sys.exit(-1)
  if type(x) is asyncio.Task:
    x = await x
  if not type(x) is list:
    return [x]
  result = []
  for y in x: result.extend(await flatten(y))
  return result

################################################################################

async def dispatch(task, hancho_outs = set()):
  # Expand our build paths
  src_dir   = path.relpath(os.getcwd(), hancho_root)
  build_dir = path.join(expand(task.build_dir, task), src_dir)

  # Flatten will await all filename promises in any of these arrays.
  task.files_in  = await flatten(task.files_in)
  task.files_out = await flatten(task.files_out)
  task.deps      = await flatten(task.deps)

  # Early-out with no result if any of our inputs or outputs are None (failed)
  if None in task.files_in:  return None
  if None in task.files_out: return None
  if None in task.deps:      return None

  task.files_in  = [expand(f, task) for f in task.files_in]
  task.files_out = [expand(f, task) for f in task.files_out]
  task.deps      = [expand(f, task) for f in task.deps]

  # Prepend directories to filenames.
  # If they're already absolute, this does nothing.
  task.files_in  = [path.join(src_dir,f)    for f in task.files_in]
  task.files_out = [path.join(build_dir, f) for f in task.files_out]
  task.deps      = [path.join(src_dir, f)   for f in task.deps]

  # Append hancho_root to all in/out filenames.
  # If they're already absolute, this does nothing.
  task.abs_files_in  = [path.abspath(path.join(hancho_root, f)) for f in task.files_in]
  task.abs_files_out = [path.abspath(path.join(hancho_root, f)) for f in task.files_out]
  task.abs_deps      = [path.abspath(path.join(hancho_root, f)) for f in task.deps]

  # And now strip hancho_root off the absolute paths to produce the final
  # root-relative paths
  task.files_in  = [path.relpath(f, hancho_root) for f in task.abs_files_in]
  task.files_out = [path.relpath(f, hancho_root) for f in task.abs_files_out]
  task.deps      = [path.relpath(f, hancho_root) for f in task.abs_deps]

  # Check for duplicate task outputs
  for file in task.abs_files_out:
    if file in hancho_outs:
      log(f"Multiple rules build {file}!")
      return None
    hancho_outs.add(file)

  # Check for valid command
  if not task.command:
    log(f"Command missing for input {task.files_in}!")
    return None

  # Check if we need a rebuild
  reason = needs_rerun(task)
  if config.force or task.force: reason = f"Files {task.abs_files_out} forced to rebuild"
  if not reason: return task.abs_files_out

  # Print the status line
  this.node_visit += 1
  complete = this.node_visit
  pending = len(asyncio.all_tasks()) - 1
  command = expand(task.command, task) if type(task.command) is str else "<callback>"
  desc    = expand(task.desc, task) if task.desc else command
  quiet   = (config.quiet or task.quiet) and not (config.verbose or config.debug)

  #log(f"[{complete:4}:{pending:4}] {desc}",
  log(f"[{complete:4}] {desc}",
      sameline = sys.stdout.isatty() and not config.multiline)

  if config.debug:
    log(f"Rebuild reason: {reason}")

  if config.debug:
    log(task)

  # Make sure our output directories exist
  for file_out in task.abs_files_out:
    if dirname := path.dirname(file_out):
      os.makedirs(dirname, exist_ok = True)

  # Flush before we run the task so that the debug output above appears in order
  sys.stdout.flush()

  # Early-exit if this is just a dry run
  if config.dryrun:
    sys.stdout.flush()
    return task.abs_files_out

  # OK, we're ready to start the task. Grab a semaphore so we don't run too
  # many at once.
  async with proc_sem:
    global total_commands
    total_commands += 1
    if type(task.command) is str:
      quiet = (config.quiet or task.quiet) and not (config.verbose or config.debug)
      if config.verbose or config.debug:
        log(f"{command}")

      # In serial mode we run the subprocess synchronously.
      if config.serial:
        result = subprocess.run(
          command,
          shell = True,
          stdout = subprocess.PIPE,
          stderr = subprocess.PIPE)
        task.stdout = result.stdout.decode()
        task.stderr = result.stderr.decode()
        task.returncode = result.returncode

      # In parallel mode we dispatch the subprocess via asyncio and then await
      # the result.
      else:
        proc = await asyncio.create_subprocess_shell(
          command,
          stdout = asyncio.subprocess.PIPE,
          stderr = asyncio.subprocess.PIPE)
        (stdout_data, stderr_data) = await proc.communicate()
        task.stdout = stdout_data.decode()
        task.stderr = stderr_data.decode()
        task.returncode = proc.returncode

      # Print command output if needed
      if not quiet and (task.stdout or task.stderr):
        if task.stderr: log(task.stderr, end="")
        if task.stdout: log(task.stdout, end="")

    elif callable(task.command):
      await task.command(task)
    else:
      log(f"Don't know what to do with {task.command}")
      sys.exit(-1)

  # Task complete. Check return code and return abs_files_out if we succeeded,
  # which will resolve the task's promise.
  if task.returncode:
    log(f"\x1B[31mFAILED\x1B[0m: {command}")
    sys.stdout.flush()
    this.any_failed = True
    return None

  if task.files_in and task.files_out:
    reason = needs_rerun(task)
    if reason:
      log(f"Task \"{desc}\" still needs rerun after running! - {reason}")
      sys.stdout.flush()
      this.any_failed = True
      return None

  sys.stdout.flush()
  return task.abs_files_out

################################################################################

def queue2(task):
  if task.files_in is None:
    print ("no files_in")
    print(task)
    sys.exit(-1)
  if task.files_out is None:
    print ("no files_out")
    print(task)
    sys.exit(-1)

  task.meta_deps = stack_deps()
  promise = dispatch(task)
  return asyncio.create_task(promise)

################################################################################

init()
if __name__ == "__main__": main()
