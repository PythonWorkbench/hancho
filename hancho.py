#!/usr/bin/python3

"""Hancho is a simple, pleasant build system."""

import argparse
import asyncio
import builtins
import inspect
import io
import json
import os
import re
import subprocess
import sys
import types
from os import path

# If we were launched directly, a reference to this module is already in
# sys.modules[__name__]. Stash another reference in sys.modules["hancho"] so
# that build.hancho and descendants don't try to load a second copy of Hancho.

this = sys.modules[__name__]
sys.modules["hancho"] = this

################################################################################
# Build rule helper methods


def color(red=None, green=None, blue=None):
    """Converts RGB color to ANSI format string"""
    if red is None:
        return "\x1B[0m"
    return f"\x1B[38;2;{red};{green};{blue}m"


def is_atom(element):
    """Returns True if 'element' should _not_ be flattened out"""
    return isinstance(element, str) or not hasattr(element, "__iter__")


def join(elements, delim=" "):
    """
    Flattens 'elements', converts elements to strings, and joins all non-None
    elements with 'delim'
    """
    return delim.join([str(y) for y in flatten(elements) if y is not None])


def run_cmd(cmd):
    """Runs a console command and returns its stdout with whitespace stripped"""
    return subprocess.check_output(cmd, shell=True, text=True).strip()


def swap_ext(name, new_ext):
    """
    Replaces file extensions on either a single filename or a list of filenames
    """
    if is_atom(name):
        return path.splitext(name)[0] + new_ext
    return [swap_ext(n, new_ext) for n in flatten(name)]


def mtime(filename):
    """Calls path.mtime and tracks how many times we called it"""
    this.mtime_calls += 1
    return path.getmtime(filename)


def flatten(elements):
    """
    Converts an arbitrarily-nested list 'elements' into a flat list, or wraps it
    in [] if it's not a list.
    """
    if is_atom(elements):
        return [elements]
    result = []
    for element in elements:
        result.extend(flatten(element))
    return result


async def flatten_async(elements):
    """Same as flatten(), except it awaits anything that needs awaiting."""
    if inspect.isawaitable(elements):
        elements = await elements
    if is_atom(elements):
        return [elements]
    result = []
    for element in elements:
        result.extend(await flatten_async(element))
    return result


def maybe_as_number(text):
    """
    Tries to convert a string to an int, then a float, then gives up. Used for
    ingesting unrecognized flag values.
    """
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


################################################################################

this.line_dirty = False


def log(message, *args, task=None, do_expand=False, sameline=False, **kwargs):
    """Simple logger that can do same-line log messages like Ninja"""
    if this.config.quiet:
        return

    if not sys.stdout.isatty():
        sameline = False

    output = io.StringIO()
    if sameline:
        kwargs["end"] = ""
    if task and do_expand:
        message = task.expand(message)
    print(message, *args, file=output, **kwargs)
    output = output.getvalue()

    if not sameline and this.line_dirty:
        sys.stdout.write("\n")
        this.line_dirty = False

    if not output:
        return

    if sameline:
        sys.stdout.write("\r")
        output = output[: os.get_terminal_size().columns - 1]
        sys.stdout.write(output)
        sys.stdout.write("\x1B[K")
    else:
        sys.stdout.write(output)

    sys.stdout.flush()
    this.line_dirty = output[-1] != "\n"


################################################################################


def main():
    """
    Our main() just handles command line args and delegates to async_main()
    """

    # pylint: disable=line-too-long
    # fmt: off
    parser = argparse.ArgumentParser()
    parser.add_argument("filename",        default="build.hancho", type=str, nargs="?", help="The name of the .hancho file to build")
    parser.add_argument("-C", "--chdir",   default="",             type=str,            help="Change directory first")
    parser.add_argument("-j", "--jobs",    default=os.cpu_count(), type=int,            help="Run N jobs in parallel (default = cpu_count, 0 = infinity)")
    parser.add_argument("-v", "--verbose", default=False,          action="store_true", help="Print verbose build info")
    parser.add_argument("-q", "--quiet",   default=False,          action="store_true", help="Mute all output")
    parser.add_argument("-n", "--dryrun",  default=False,          action="store_true", help="Do not run commands")
    parser.add_argument("-d", "--debug",   default=False,          action="store_true", help="Print debugging information")
    parser.add_argument("-f", "--force",   default=False,          action="store_true", help="Force rebuild of everything")
    # fmt: on

    (flags, unrecognized) = parser.parse_known_args()

    # We set this to None first so that this.config.base gets sets to None in
    # the next line.
    this.config = None

    this.config = Rule(
        filename="build.hancho",
        chdir=None,
        jobs=os.cpu_count(),
        verbose=False,
        quiet=False,
        dryrun=False,
        debug=False,
        force=False,
        desc="{files_in} -> {files_out}",
        build_dir="build",
        files_out=[],
        deps=[],
        expand=expand,
        join=join,
        len=len,
        run_cmd=run_cmd,
        swap_ext=swap_ext,
        color=color,
    )

    this.config |= flags.__dict__

    # Unrecognized flags become global config fields.
    for span in unrecognized:
        if match := re.match(r"-+([^=\s]+)(?:=(\S+))?", span):
            this.config[match.group(1)] = (
                maybe_as_number(match.group(2)) if match.group(2) is not None else True
            )

    return asyncio.run(async_main())


################################################################################


async def async_main():
    """All the actual Hancho stuff runs in an async context."""

    # Reset all global state
    this.hancho_root = os.getcwd()
    this.hancho_mods = {}
    this.mod_stack = []
    this.hancho_outs = set()
    this.tasks_total = 0
    this.tasks_pass = 0
    this.tasks_fail = 0
    this.tasks_skip = 0
    this.task_counter = 0
    this.mtime_calls = 0

    # Change directory and load top module(s).
    if not path.exists(this.config.filename):
        raise FileNotFoundError(f"Could not find {this.config.filename}")

    if this.config.chdir:
        os.chdir(this.config.chdir)
    load_abs(path.abspath(this.config.filename))

    # Top module(s) loaded. Configure our job semaphore and run all tasks in the
    # queue until we run out.
    if not this.config.jobs:
        this.config.jobs = 1000
    this.semaphore = asyncio.Semaphore(this.config.jobs)

    while True:
        pending_tasks = asyncio.all_tasks() - {asyncio.current_task()}
        if not pending_tasks:
            break
        await asyncio.wait(pending_tasks)

    # Done, print status info if needed
    if this.config.debug:
        log(f"tasks total:   {this.tasks_total}")
        log(f"tasks passed:  {this.tasks_pass}")
        log(f"tasks failed:  {this.tasks_fail}")
        log(f"tasks skipped: {this.tasks_skip}")
        log(f"mtime calls:   {this.mtime_calls}")

    if this.tasks_fail:
        log("hancho: \x1B[31mBUILD FAILED\x1B[0m")
    elif this.tasks_pass:
        log("hancho: \x1B[32mBUILD PASSED\x1B[0m")
    else:
        log("hancho: \x1B[33mBUILD CLEAN\x1B[0m")

    if this.config.chdir:
        os.chdir(this.hancho_root)
    return -1 if this.tasks_fail else 0


################################################################################
# The .hancho file loader does a small amount of work to keep track of the
# stack of .hancho files that have been loaded.


def load(mod_path):
    """
    Searches the loaded Hancho module stack for a module whose directory
    contains 'mod_path', then loads the module relative to that path.
    """
    for parent_mod in reversed(this.mod_stack):
        abs_path = path.abspath(path.join(path.split(parent_mod.__file__)[0], mod_path))
        if os.path.exists(abs_path):
            return load_abs(abs_path)
    raise FileNotFoundError(f"Could not load module {mod_path}")


def load_abs(abs_path):
    """
    Loads a Hancho module ***while chdir'd into its directory***
    """
    if abs_path in this.hancho_mods:
        return this.hancho_mods[abs_path]

    mod_dir = path.split(abs_path)[0]
    mod_file = path.split(abs_path)[1]
    mod_name = mod_file.split(".")[0]

    header = "from hancho import *\n"
    with open(abs_path, encoding="utf-8") as file:
        source = header + file.read()
        code = compile(source, abs_path, "exec", dont_inherit=True)

    module = type(sys)(mod_name)
    module.__file__ = abs_path
    module.__builtins__ = builtins
    this.hancho_mods[abs_path] = module

    sys.path.insert(0, mod_dir)
    old_dir = os.getcwd()

    # We must chdir()s into the .hancho file directory before running it so that
    # glob() can resolve files relative to the .hancho file itself.
    this.mod_stack.append(module)
    os.chdir(mod_dir)

    # Why Pylint thinks is not callable is a mystery.
    types.FunctionType(code, module.__dict__)()  # pylint: disable=not-callable

    os.chdir(old_dir)
    this.mod_stack.pop()

    return module


################################################################################

template_regex = re.compile("{[^}]*}")


def expand_once(rule, template):
    """
    Does one pass of template expansion on 'template' using fields from 'rule'.
    Exceptions during expansion are _not_ an error, instead they cause the
    template to be copied unexpanded to the output.
    """
    if template is None:
        return ""
    result = ""
    while span := template_regex.search(template):
        result += template[0 : span.start()]
        exp = template[span.start() : span.end()]
        try:
            replacement = eval(exp[1:-1], globals(), rule)  # pylint: disable=eval-used
            if replacement is not None:
                result += join(replacement)
        except Exception:  # pylint: disable=broad-except
            result += exp
        template = template[span.end() :]
    result += template
    return result


def expand(rule, template):
    """
    A trivial templating system that replaces {foo} with the value of rule.foo
    and keeps going until it can't replace anything. Templates that evaluate to
    None are replaced with the empty string.
    """
    if isinstance(template, list):
        return [expand(rule, t) for t in template]

    for _ in range(100):
        if rule.debug:
            log(f'expand "{template}"')
        new_template = expand_once(rule, template)
        if template == new_template:
            if template_regex.search(template):
                raise ValueError(f"Expanding '{template[0:20]}' is stuck in a loop")
            return template
        template = new_template
    raise ValueError(f"Expanding '{template[0:20]}...' failed to terminate")


################################################################################
# We have to disable 'attribute-defined-outside-init' because of the attribute
# inheritance we're implementing through '__missing__' - if we define
# everything in __init__, __missing__ won't fire and we won't see the base
# instance's version of that attribute.
# pylint: disable=attribute-defined-outside-init

class Rule(dict):
    """
    Hancho's Rule object behaves like a Javascript object and implements a basic
    form of prototypal inheritance via Rule.base
    """

    # pylint: disable=too-many-instance-attributes
    def __init__(self, *, base=None, **kwargs):
        super().__init__(self)
        self |= kwargs
        self.base = this.config if base is None else base

    def __missing__(self, key):
        if self.base:
            return self.base[key]
        return None

    def __setattr__(self, key, value):
        self.__setitem__(key, value)

    def __getattr__(self, key):
        return self.__getitem__(key)

    def __repr__(self):
        """Turns this rule into a JSON doc for debugging"""

        class Encoder(json.JSONEncoder):
            """Turns functions and tasks into stub strings for dumping."""

            def default(self, o):
                if callable(o):
                    return "<function>"
                if isinstance(o, asyncio.Task):
                    return "<task>"
                return super().default(o)

        return json.dumps(self, indent=2, cls=Encoder)

    def extend(self, **kwargs):
        """
        Returns a 'subclass' of this Rule that can override this rule's fields.
        """
        return Rule(base=self, **kwargs)

    def expand(self, template):
        """Expands a template string using fields from this rule."""
        return expand(self, template)

    def __call__(self, files_in, files_out=None, **kwargs):
        this.tasks_total += 1
        task = self.extend()
        task.files_in = files_in
        if files_out is not None:
            task.files_out = files_out
        task.abs_cwd = path.split(this.mod_stack[-1].__file__)[0]
        task |= kwargs
        promise = task.async_call()
        return asyncio.create_task(promise)

    ########################################

    async def async_call(self):
        """Entry point for async task stuff."""
        try:
            result = await self.dispatch()
            return result
        except Exception as err:  # pylint: disable=broad-except
            log(f"Task '{self.expand(self.desc)}' failed:")
            log(f"{color(255, 128, 128)}{err}{color()}")
            this.tasks_fail += 1
            return None

    ########################################

    async def dispatch(self):
        """Does all the bookkeeping and depedency checking, then runs the command if needed."""
        # Check for missing fields
        if not self.command:
            raise ValueError(f"Command missing for input {self.files_in}!")
        if self.files_in is None:
            raise ValueError(f"Task {self.desc} missing files_in")
        if self.files_out is None:
            raise ValueError(f"Task {self.desc} missing files_out")

        # Wait for all our deps
        await self.await_paths()

        # Deps fulfilled, we are now runnable so grab a task index.
        this.task_counter += 1
        self.task_index = this.task_counter

        # Check for duplicate task outputs
        for file in self.abs_files_out:
            if file in this.hancho_outs:
                raise Exception(f"Multiple rules build {file}!")
            this.hancho_outs.add(file)

        # Check if we need a rebuild
        self.reason = self.needs_rerun()
        if not self.reason:
            this.tasks_skip += 1
            return self.abs_files_out

        # Make sure our output directories exist
        for file_out in self.abs_files_out:
            if dirname := path.dirname(file_out):
                os.makedirs(dirname, exist_ok=True)

        # OK, we're ready to start the task.
        async with this.semaphore:
            self.print_status()
            result = []
            for command in flatten(self.command):
                result = await self.run_command(command)

        # Task complete, check if it actually updated all the output files
        if self.files_in and self.files_out:
            if second_reason := self.needs_rerun():
                raise Exception(
                    f"Task '{self.expand(self.desc)}' still needs rerun after running!\n"
                    + f"Reason: {second_reason}"
                )

        this.tasks_pass += 1
        return result

    ########################################

    async def await_paths(self):
        """Awaits, expands, and normalizes all paths in this task"""

        # Flatten all filename promises in any of the input filename arrays.
        self.files_in = await flatten_async(self.files_in)
        self.files_out = await flatten_async(self.files_out)
        self.deps = await flatten_async(self.deps)

        # Early-out if any of our inputs or outputs are None (failed)
        if None in self.files_in:
            raise Exception("One of our inputs failed")
        if None in self.files_out:
            raise Exception("Somehow we have a None in our outputs")
        if None in self.deps:
            raise Exception("One of our deps failed")

        # Do the actual template expansion to produce real filename lists
        self.files_in = self.expand(self.files_in)
        self.files_out = self.expand(self.files_out)
        self.deps = self.expand(self.deps)

        # Prepend directories to filenames and then normalize + absolute them.
        # If they're already absolute, this does nothing.
        src_dir = path.relpath(self.abs_cwd, this.hancho_root)
        build_dir = path.join(self.expand(self.build_dir), src_dir)

        self.abs_files_in = [
            path.abspath(path.join(this.hancho_root, src_dir, f)) for f in self.files_in
        ]
        self.abs_files_out = [
            path.abspath(path.join(this.hancho_root, build_dir, f))
            for f in self.files_out
        ]
        self.abs_deps = [
            path.abspath(path.join(this.hancho_root, src_dir, f)) for f in self.deps
        ]

        # Strip hancho_root off the absolute paths to produce root-relative paths
        self.files_in = [path.relpath(f, this.hancho_root) for f in self.abs_files_in]
        self.files_out = [path.relpath(f, this.hancho_root) for f in self.abs_files_out]
        self.deps = [path.relpath(f, this.hancho_root) for f in self.abs_deps]

    ########################################

    def print_status(self):
        """Print the "[1/N] Foo foo.foo foo.o" status line and debug information"""
        log(
            f"[{self.task_index}/{this.tasks_total}] {self.expand(self.desc)}",
            sameline=not self.verbose,
        )
        if self.verbose or self.debug:
            log(f"Reason: {self.reason}")
            for command in flatten(self.command):
                if isinstance(command, str):
                    log(f"{self.expand(command)}")
            if self.debug:
                log(self)

    ########################################

    async def run_command(self, command):
        """Actually runs a command, either by calling it or running it in a subprocess"""

        # Early exit if this is just a dry run
        if self.dryrun:
            return self.abs_files_out

        # Custom commands just get await'ed and then early-out'ed.
        if callable(command):
            result = await command(self)
            if result is None:
                raise Exception(f"{command} returned None")
            return result

        # Non-string non-callable commands are not valid
        if not isinstance(command, str):
            raise ValueError(f"Don't know what to do with {command}")

        # Create the subprocess via asyncio and then await the result.
        proc = await asyncio.create_subprocess_shell(
            self.expand(command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        (stdout_data, stderr_data) = await proc.communicate()

        self.stdout = stdout_data.decode()
        self.stderr = stderr_data.decode()
        self.returncode = proc.returncode

        # Print command output if needed
        if not self.quiet and (self.stdout or self.stderr):
            if self.stderr:
                log(self.stderr, end="")
            if self.stdout:
                log(self.stdout, end="")

        # Task complete, check the task return code
        if self.returncode:
            raise Exception(f"{command} exited with return code {self.returncode}")

        # Task passed, return the output file list
        return self.abs_files_out

    ########################################
    # Pylint really doesn't like this function, lol.
    # pylint: disable=too-many-return-statements,too-many-branches

    def needs_rerun(self):
        """Checks if a task needs to be re-run, and returns a non-empty reason if so."""
        files_in = self.abs_files_in
        files_out = self.abs_files_out

        if self.force:
            return f"Files {self.files_out} forced to rebuild"
        if not files_in:
            return "Always rebuild a target with no inputs"
        if not files_out:
            return "Always rebuild a target with no outputs"

        # Tasks with missing outputs always run.
        for file_out in files_out:
            if not path.exists(file_out):
                return f"Rebuilding {self.files_out} because some are missing"

        min_out = min(mtime(f) for f in files_out)

        # Check the hancho file(s) that generated the task
        if max(mtime(f) for f in this.hancho_mods.keys()) >= min_out:
            return f"Rebuilding {self.files_out} because its .hancho files have changed"

        # Check user-specified deps.
        if self.deps and max(mtime(f) for f in self.deps) >= min_out:
            return (
                f"Rebuilding {self.files_out} because a manual dependency has changed"
            )

        # Check GCC-format depfile, if present.
        if self.depfile:
            abs_depfile = path.abspath(
                path.join(this.hancho_root, self.expand(self.depfile))
            )
            if path.exists(abs_depfile):
                if self.debug:
                    log(f"Found depfile {abs_depfile}")
                with open(abs_depfile, encoding="utf-8") as depfile:
                    deplines = depfile.read().split()
                    deplines = [d for d in deplines[1:] if d != "\\"]
                    if deplines and max(mtime(f) for f in deplines) >= min_out:
                        return (
                            f"Rebuilding {self.files_out} because a dependency in "
                            + f"{abs_depfile} has changed"
                        )

        # Check input files.
        if files_in and max(mtime(f) for f in files_in) >= min_out:
            return f"Rebuilding {self.files_out} because an input has changed"

        # All checks passed, so we don't need to rebuild this output.
        if self.debug:
            log(f"Files {self.files_out} are up to date")

        # All deps were up-to-date, nothing to do.
        return None


################################################################################

if __name__ == "__main__":
    sys.exit(main())
