#!/usr/bin/python3
# pylint: disable=too-many-lines

"""Hancho v0.4.0 @ 2024-11-01 - A simple, pleasant build system."""

from os import path
import argparse
import asyncio
import builtins
import copy
import glob
import inspect
import io
import json
import os
import random
import re
import subprocess
import sys
import time
import traceback
import types
from collections import abc

# If we were launched directly, a reference to this module is already in sys.modules[__name__].
# Stash another reference in sys.modules["hancho"] so that build.hancho and descendants don't try
# to load a second copy of Hancho.
sys.modules["hancho"] = sys.modules[__name__]

# ---------------------------------------------------------------------------------------------------
# Logging stuff

first_line_block = True


def log_line(message):
    app.log += message
    if not app.flags.quiet:
        sys.stdout.write(message)
        sys.stdout.flush()


def log(message, *, sameline=False, **kwargs):
    """Simple logger that can do same-line log messages like Ninja."""
    if not sys.stdout.isatty():
        sameline = False

    if sameline:
        kwargs.setdefault("end", "")

    output = io.StringIO()
    print(message, file=output, **kwargs)
    output = output.getvalue()

    if not output:
        return

    if sameline:
        output = output[: os.get_terminal_size().columns - 1]
        output = "\r" + output + "\x1B[K"
        log_line(output)
    else:
        if app.line_dirty:
            log_line("\n")
        log_line(output)

    app.line_dirty = sameline


def line_block(lines):
    count = len(lines)
    global first_line_block # pylint: disable=global-statement
    if not first_line_block:
        print(f"\x1b[{count}A")
    else:
        first_line_block = False
    for y in range(count):
        if y > 0:
            print()
        line = lines[y]
        if line is not None:
            line = line[: os.get_terminal_size().columns - 20]
        print(line, end="")
        print("\x1b[K", end="")
        sys.stdout.flush()


def log_exception():
    log(color(255, 128, 128), end="")
    log(traceback.format_exc())
    log(color(), end="")


# ---------------------------------------------------------------------------------------------------
# Path manipulation


def abs_path(raw_path, strict=False) -> str | list[str]:

    if listlike(raw_path):
        return [abs_path(p, strict) for p in raw_path]

    result = path.realpath(raw_path)
    if strict and not path.exists(result):
        raise FileNotFoundError(raw_path)
    return result


def rel_path(path1, path2):

    if listlike(path1):
        return [rel_path(p, path2) for p in path1]

    # Generating relative paths in the presence of symlinks doesn't work with either
    # Path.relative_to or os.path.relpath - the former balks at generating ".." in paths, the
    # latter does generate them but "path/with/symlink/../foo" doesn't behave like you think it
    # should. What we really want is to just remove redundant cwd stuff off the beginning of the
    # path, which we can do with simple string manipulation.
    return path1.removeprefix(path2 + "/") if path1 != path2 else "."


def join_path(path1, path2, *args):
    result = join_path2(path1, path2, *args)
    return flatten(result) if listlike(result) else result


def join_path2(path1, path2, *args):

    if len(args) > 0:
        return [join_path(path1, p) for p in join_path(path2, *args)] # pylint: disable=E1120
    if listlike(path1):
        return [join_path(p, path2) for p in flatten(path1)]
    if listlike(path2):
        return [join_path(path1, p) for p in flatten(path2)]

    if not path2:
        raise ValueError(f"Cannot join '{path1}' with '{type(path2)}' == '{path2}'")
    return path.join(path1, path2)


def normalize_path(file_path):
    assert isinstance(file_path, str)
    assert not macro_regex.search(file_path)

    file_path = path.realpath(path.join(os.getcwd(), file_path))
    assert path.isabs(file_path)
    if not path.isfile(file_path):
        print(f"Could not find file {file_path}")
        assert path.isfile(file_path)

    return file_path


# ---------------------------------------------------------------------------------------------------
# Helper methods


def listlike(variant):
    return isinstance(variant, abc.Sequence) and not isinstance(
        variant, (str, bytes, bytearray)
    )


def dictlike(variant):
    return isinstance(variant, abc.Mapping)


def flatten(variant):
    if listlike(variant):
        return [x for element in variant for x in flatten(element)]
    elif variant is None:
        return []
    return [variant]


def join_prefix(prefix, strings):
    return [prefix + str(s) for s in flatten(strings)]


def join_suffix(strings, suffix):
    return [str(s) + suffix for s in flatten(strings)]


def stem(filename):
    filename = flatten(filename)[0]
    filename = path.basename(filename)
    return path.splitext(filename)[0]


def color(red=None, green=None, blue=None):
    """Converts RGB color to ANSI format string."""
    # Color strings don't work in Windows console, so don't emit them.
    # if not Config.use_color or os.name == "nt":
    #    return ""
    if red is None:
        return "\x1B[0m"
    return f"\x1B[38;2;{red};{green};{blue}m"


def run_cmd(cmd):
    """Runs a console command synchronously and returns its stdout with whitespace stripped."""
    return subprocess.check_output(cmd, shell=True, text=True).strip()


def swap_ext(name, new_ext):
    """Replaces file extensions on either a single filename or a list of filenames."""
    if isinstance(name, Task):
        name = name.out_files
    if listlike(name):
        return [swap_ext(n, new_ext) for n in name]
    return path.splitext(name)[0] + new_ext


def mtime(filename):
    """Gets the file's mtime and tracks how many times we've called mtime()"""
    app.mtime_calls += 1
    return os.stat(filename).st_mtime_ns


def maybe_as_number(text):
    """Tries to convert a string to an int, then a float, then gives up. Used for ingesting
    unrecognized flag values."""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


####################################################################################################
# Heplers for managing variants (could be Config, list, dict, etc.)


def merge_variant(lhs, rhs):
    if isinstance(lhs, Config) and dictlike(rhs):
        for key, rval in rhs.items():
            lval = lhs.get(key, None)
            if lval is None or rval is not None:
                lhs[key] = merge_variant(lval, rval)
        return lhs
    return copy.deepcopy(rhs)


def map_variant(key, val, apply):
    val = apply(key, val)
    if dictlike(val):
        for key2, val2 in val.items():
            val[key2] = map_variant(key2, val2, apply)
    elif listlike(val):
        for key2, val2 in enumerate(val):
            val[key2] = map_variant(key2, val2, apply)
    return val


async def await_variant(variant):
    """Recursively replaces every awaitable in the variant with its awaited value."""

    if isinstance(variant, Promise):
        variant = await variant.get()
        variant = await await_variant(variant)
    elif isinstance(variant, Task):
        await variant.await_done()
        variant = await await_variant(variant.out_files)
    elif dictlike(variant):
        for key, val in variant.items():
            variant[key] = await await_variant(val)
    elif listlike(variant):
        for key, val in enumerate(variant):
            variant[key] = await await_variant(val)
    else:
        while inspect.isawaitable(variant):
            variant = await variant

    return variant


####################################################################################################


class Dumper:
    def __init__(self, max_depth=2):
        self.depth = 0
        self.max_depth = max_depth

    def indent(self):
        return "  " * self.depth

    def dump(self, variant):
        result = f"{type(variant).__name__} @ {hex(id(variant))} "
        if isinstance(variant, Task):
            result += self.dump_dict(variant.__dict__)
        elif isinstance(variant, HanchoAPI):
            result += self.dump_dict(variant.__dict__)
        elif isinstance(variant, Config):
            result += self.dump_dict(variant)
        elif listlike(variant):
            result += self.dump_list(variant)
        elif dictlike(variant):
            result = ""
            result += self.dump_dict(variant)
        elif isinstance(variant, str):
            result = ""
            result += '"' + str(variant) + '"'
        else:
            result = ""
            result += str(variant)
        return result

    def dump_list(self, l):
        if len(l) == 0:
            return "[]"

        if len(l) == 1:
            return f"[{self.dump(l[0])}]"

        if self.depth >= self.max_depth:
            return "[...]"

        result = "[\n"
        self.depth += 1
        for val in l:
            result += self.indent() + self.dump(val) + ",\n"
        self.depth -= 1
        result += self.indent() + "]"
        return result

    def dump_dict(self, d):
        if self.depth >= self.max_depth:
            return "{...}"

        result = "{\n"
        self.depth += 1
        for key, val in d.items():
            result += self.indent() + f"{key} = " + self.dump(val) + ",\n"
        self.depth -= 1
        result += self.indent() + "}"
        return result


####################################################################################################


class Utils:
    # fmt: off
    abs_path    = staticmethod(abs_path)
    rel_path    = staticmethod(rel_path)
    join_path   = staticmethod(join_path)
    color       = staticmethod(color)
    glob        = staticmethod(glob.glob)
    len         = staticmethod(len)
    run_cmd     = staticmethod(run_cmd)
    swap_ext    = staticmethod(swap_ext)
    flatten     = staticmethod(flatten)
    print       = staticmethod(print)
    log         = staticmethod(log)
    path        = path
    re          = re
    join_prefix = staticmethod(join_prefix)
    join_suffix = staticmethod(join_suffix)
    stem        = staticmethod(stem)
    hancho_dir  = abs_path(path.dirname(path.realpath(__file__)))
    # fmt: on


####################################################################################################


class Config(dict, Utils):
    """
    A Config object is a specialized dict that also supports 'config.attribute' syntax as well as
    arbitrary "merging" of dicts/keys and text template expansion.
    """

    def __init__(self, *args, **kwargs):
        self.merge(*args)
        self.merge(kwargs)

    def __repr__(self):
        return Dumper(2).dump(self)

    def __getattr__(self, key):
        if not dict.__contains__(self, key):
            raise AttributeError(name=key, obj=self)
        result = dict.__getitem__(self, key)
        return result

    def __setattr__(self, key, val):
        return dict.__setitem__(self, key, val)

    def __delattr__(self, key):
        if not dict.__contains__(self, key):
            raise AttributeError(name=key, obj=self)
        return dict.__delitem__(self, key)

    # ----------------------------------------

    def fork(self, *args, **kwargs):
        return type(self)(self, *args, **kwargs)

    def merge(self, *args, **kwargs):
        for arg in flatten(args):
            if arg is not None:
                assert dictlike(arg)
                merge_variant(self, arg)
        merge_variant(self, kwargs)
        return self

    def expand(self, variant):
        return expand_variant(Expander(self), variant)

    def rel(self, sub_path):
        return rel_path(sub_path, expand_variant(self, self.task_dir))


####################################################################################################
# Hancho's text expansion system. Works similarly to Python's F-strings, but with quite a bit more
# power.
#
# The code here requires some explanation.
#
# We do not necessarily know in advance how the users will nest strings, macros, callbacks,
# etcetera. Text expansion therefore requires dynamic-dispatch-type stuff to ensure that we always
# end up with flat strings.
#
# The result of this is that the functions here are mutually recursive in a way that can lead to
# confusing callstacks, but that should handle every possible case of stuff inside other stuff.
#
# The depth checks are to prevent recursive runaway - the MAX_EXPAND_DEPTH limit is arbitrary but
# should suffice.
#
# Also - TEFINAE - Text Expansion Failure Is Not An Error. Config objects can contain macros that
# are not expandable inside the config. This allows config objects nested inside other configs to
# contain templates that can only be expanded in the context of the outer config, and things will
# still Just Work.

# The maximum number of recursion levels we will do to expand a macro.
# Tests currently require MAX_EXPAND_DEPTH >= 6
MAX_EXPAND_DEPTH = 20

# Matches macros inside a string.
macro_regex = re.compile("{[^{}]*}")

# ----------------------------------------
# Helper methods


def trace_prefix(expander):
    """Prints the left-side trellis of the expansion traces."""
    assert isinstance(expander, Expander)
    return hex(id(expander.config)) + ": " + ("┃ " * app.expand_depth)


def trace_variant(variant):
    """Prints the right-side values of the expansion traces."""
    if callable(variant):
        return f"Callable @ {hex(id(variant))}"
    elif isinstance(variant, Config):
        return f"Config @ {hex(id(variant))}'"
    elif isinstance(variant, Expander):
        return f"Expander @ {hex(id(variant.config))}'"
    else:
        return f"'{variant}'"


def expand_inc():
    """Increments the current expansion recursion depth."""
    app.expand_depth += 1
    if app.expand_depth > MAX_EXPAND_DEPTH:
        raise RecursionError("Text expansion failed to terminate")


def expand_dec():
    """Decrements the current expansion recursion depth."""
    app.expand_depth -= 1
    if app.expand_depth < 0:
        raise RecursionError("Text expand_inc/dec unbalanced")


def stringify_variant(variant):
    """Converts any type into an expansion-compatible string."""
    if variant is None:
        return ""
    elif isinstance(variant, Expander):
        return stringify_variant(variant.config)
    elif isinstance(variant, Task):
        return stringify_variant(variant.out_files)
    elif listlike(variant):
        variant = [stringify_variant(val) for val in variant]
        return " ".join(variant)
    else:
        return str(variant)


class Expander:
    """Wraps a Config object and expands all fields read from it."""

    def __init__(self, config):
        self.config = config
        # We save a copy of 'trace', otherwise we end up printing traces of reading trace.... :P
        self.trace = config.get("trace", app.flags.trace)

    def __getitem__(self, key):
        return self.get(key)

    def __getattr__(self, key):
        return self.get(key)

    def get(self, key):
        try:
            # This has to be getattr() so that we also check other Config base classes like Utils.
            val = getattr(self.config, key)
        except KeyError:
            if self.trace:
                log(trace_prefix(self) + f"Read '{key}' failed")
            raise

        if self.trace:
            if key != "__iter__":
                log(trace_prefix(self) + f"Read '{key}' = {trace_variant(val)}")
        val = expand_variant(self, val)
        return val


def expand_text(expander, text):
    """Replaces all macros in 'text' with their expanded, stringified values."""

    if not macro_regex.search(text):
        return text

    if expander.trace:
        log(trace_prefix(expander) + f"┏ expand_text '{text}'")
    expand_inc()

    # ==========

    temp = text
    result = ""
    while span := macro_regex.search(temp):
        result += temp[0 : span.start()]
        macro = temp[span.start() : span.end()]
        variant = expand_macro(expander, macro)
        result += stringify_variant(variant)
        temp = temp[span.end() :]
    result += temp

    # ==========

    expand_dec()
    if expander.trace:
        log(trace_prefix(expander) + f"┗ expand_text '{text}' = '{result}'")

    # If expansion changed the text, try to expand it again.
    if result != text:
        result = expand_text(expander, result)

    return result


def expand_macro(expander, macro):
    """Evaluates the contents of a "{macro}" string. If eval throws an exception, the macro is
    returned unchanged."""

    assert isinstance(expander, Expander)

    if expander.trace:
        log(trace_prefix(expander) + f"┏ expand_macro '{macro}'")
    expand_inc()

    # ==========

    result = macro
    failed = False

    try:
        result = eval(macro[1:-1], {}, expander)  # pylint: disable=eval-used
    except BaseException:  # pylint: disable=broad-exception-caught
        failed = True

    # ==========

    expand_dec()
    if expander.trace:
        if failed:
            log(trace_prefix(expander) + f"┗ expand_macro '{macro}' failed")
        else:
            log(trace_prefix(expander) + f"┗ expand_macro '{macro}' = {result}")
    return result


def expand_variant(expander, variant):
    """Expands all macros anywhere inside 'variant', making deep copies where needed so we don't
    expand someone else's data."""

    # This level of tracing is too spammy to be useful.
    # if expander.trace:
    #   log(trace_config(expander) + f"┏ expand_variant {trace_variant(variant)}")
    # expand_inc()

    if isinstance(variant, Config):
        result = Expander(variant)
    elif listlike(variant):
        result = [expand_variant(expander, val) for val in variant]
    elif dictlike(variant):
        result = {
            expand_variant(expander, key): expand_variant(expander, val)
            for key, val in variant.items()
        }
    elif isinstance(variant, str):
        result = expand_text(expander, variant)
    else:
        result = variant

    # expand_dec()
    # if expander.trace:
    #    log(trace_config(expander) + f"┗ expand_variant {trace_variant(variant)} = {trace_variant(result)}")

    return result


####################################################################################################


class Promise:
    def __init__(self, task, *args):
        self.task = task
        self.args = args

    async def get(self):
        await self.task.await_done()
        if len(self.args) == 0:
            return self.task.out_files
        elif len(self.args) == 1:
            return self.task.config[self.args[0]]
        else:
            return [self.task.config[field] for field in self.args]


####################################################################################################


class TaskState:
    DECLARED = "DECLARED"
    QUEUED = "QUEUED"
    STARTED = "STARTED"
    AWAITING_INPUTS = "AWAITING_INPUTS"
    TASK_INIT = "TASK_INIT"
    AWAITING_JOBS = "AWAITING_JOBS"
    RUNNING_COMMANDS = "RUNNING_COMMANDS"
    FINISHED = "FINISHED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    BROKEN = "BROKEN"


####################################################################################################


class Task:

    default_desc = "{command}"
    default_command = None
    default_task_dir = "{mod_dir}"
    default_build_dir = (
        "{build_root}/{build_tag}/{repo_name}/{rel_path(task_dir, repo_dir)}"
    )
    default_build_root = "{root_dir}/build"
    default_build_tag = ""

    def __init__(self, *args, **kwargs):
        self.config = Config(
            desc=Task.default_desc,
            command=Task.default_command,
            task_dir=Task.default_task_dir,
            build_dir=Task.default_build_dir,
        )

        self.config.merge(*args, **kwargs)

        self._task_index = 0
        self.in_files = []
        self.out_files = []
        self._state = TaskState.DECLARED
        self._reason = None
        self.asyncio_task = None
        self._loaded_files = list(app.loaded_files)
        self._stdout = ""
        self._stderr = ""
        self._returncode = -1

        app.all_tasks.append(self)

    # ----------------------------------------

    # WARNING: Tasks must _not_ be copied or we'll hit the "Multiple tasks generate file X" checks.
    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __repr__(self):
        return Dumper(2).dump(self)

    # ----------------------------------------

    def queue(self):
        if self._state is TaskState.DECLARED:
            app.queued_tasks.append(self)
            self._state = TaskState.QUEUED

            def apply(_, val):
                if isinstance(val, Task):
                    val.queue()
                return val

            map_variant(None, self.config, apply)

    def start(self):
        self.queue()
        if self._state is TaskState.QUEUED:
            self.asyncio_task = asyncio.create_task(self.task_main())
            self._state = TaskState.STARTED
            app.tasks_started += 1

    async def await_done(self):
        self.start()
        await self.asyncio_task

    def promise(self, *args):
        return Promise(self, *args)

    # -----------------------------------------------------------------------------------------------

    def print_status(self):
        """Print the "[1/N] Compiling foo.cpp -> foo.o" status line and debug information"""
        verbose = self.config.get("verbose", app.flags.verbose)
        log(
            f"{color(128,255,196)}[{self._task_index}/{app.tasks_started}]{color()} {self.config.desc}",
            sameline=not verbose,
        )

    # -----------------------------------------------------------------------------------------------

    async def task_main(self):
        """Entry point for async task stuff, handles exceptions generated during task execution."""

        verbose = self.config.get("verbose", app.flags.verbose)
        debug = self.config.get("debug", app.flags.debug)
        force = self.config.get("force", app.flags.force)

        # Await everything awaitable in this task's config.
        # If any of this tasks's dependencies were cancelled, we propagate the cancellation to
        # downstream tasks.
        try:
            assert self._state is TaskState.STARTED
            self._state = TaskState.AWAITING_INPUTS
            for key, val in self.config.items():
                self.config[key] = await await_variant(val)
        except BaseException as ex:  # pylint: disable=broad-exception-caught
            # Exceptions during awaiting inputs means that this task cannot proceed, cancel it.
            self._state = TaskState.CANCELLED
            app.tasks_cancelled += 1
            raise asyncio.CancelledError() from ex

        # Everything awaited, task_init runs synchronously.
        try:
            self._state = TaskState.TASK_INIT
            self.task_init()
        except BaseException as ex:  # pylint: disable=broad-exception-caught
            self._state = TaskState.BROKEN
            app.tasks_broken += 1
            raise ex

        # Early-out if this is a no-op task
        if self.config.command is None:
            app.tasks_finished += 1
            self._state = TaskState.FINISHED
            return

        # Check if we need a rebuild
        self._reason = self.needs_rerun(force)
        if not self._reason:
            app.tasks_skipped += 1
            self._state = TaskState.SKIPPED
            return
        elif verbose or debug:
            log(f"{color(128,128,128)}Reason: {self._reason}{color()}")

        try:
            # Wait for enough jobs to free up to run this task.
            job_count = self.config.get("job_count", 1)
            self._state = TaskState.AWAITING_JOBS
            await app.job_pool.acquire_jobs(job_count, self)

            # Run the commands.
            self._state = TaskState.RUNNING_COMMANDS
            app.tasks_running += 1
            self._task_index = app.tasks_running
            self.print_status()

            for command in flatten(self.config.command):
                await self.run_command(command)
                if self._returncode != 0:
                    break

        except BaseException as ex:  # pylint: disable=broad-exception-caught
            # If any command failed, we print the error and propagate it to downstream tasks.
            self._state = TaskState.FAILED
            app.tasks_failed += 1
            raise ex
        finally:
            await app.job_pool.release_jobs(job_count, self)

        # Task finished successfully
        self._state = TaskState.FINISHED
        app.tasks_finished += 1

    # -----------------------------------------------------------------------------------------------

    def task_init(self):
        """All the setup steps needed before we run a task."""

        debug = self.config.get("debug", app.flags.debug)

        if debug:
            log(f"\nTask before expand: {self.config}")

        # ----------------------------------------
        # Expand task_dir and build_dir

        # pylint: disable=attribute-defined-outside-init
        self.config.task_dir = abs_path(self.config.expand(self.config.task_dir))
        self.config.build_dir = abs_path(self.config.expand(self.config.build_dir))

        # ----------------------------------------
        # Expand all in_ and out_ filenames.
        # We _must_ expand these first before joining paths or the paths will be incorrect:
        # prefix + swap(abs_path) != abs(prefix + swap(path))

        for key, val in self.config.items():
            if key.startswith("in_") or key.startswith("out_"):
                self.config[key] = self.config.expand(val)

        # ----------------------------------------
        # Convert all in_, out_, and deps filenames to absolute paths.

        def join_dir(key, val):
            if isinstance(key, str) and val is not None:
                if key == "in_depfile":
                    val = join_path(self.config.build_dir, val)
                    val = abs_path(val)
                    # Note - we only add the depfile to in_files _if_it_exists_, otherwise we will
                    # fail a check that all our inputs are present.
                    if path.isfile(val):
                        self.in_files.append(val)
                elif key.startswith("out_"):
                    val = join_path(self.config.build_dir, val)
                    val = abs_path(val)
                    self.out_files.extend(flatten(val))
                elif key.startswith("in_"):
                    val = join_path(self.config.task_dir, val)
                    val = abs_path(val)
                    self.in_files.extend(flatten(val))
            return val

        map_variant(None, self.config, join_dir)

        # ----------------------------------------
        # And now we can expand the command.

        self.config.desc = self.config.expand(self.config.desc)
        self.config.command = self.config.expand(self.config.command)

        if debug:
            log(f"\nTask after expand: {self.config}")

        # ----------------------------------------
        # Sanity checks

        # Check for missing input files/paths
        if not path.exists(self.config.task_dir):
            raise FileNotFoundError(self.config.task_dir)

        for file in self.in_files:
            if file is None:
                raise ValueError("in_files contained a None")
            if not path.exists(file):
                raise FileNotFoundError(file)

        # Check that all build files would end up under root_dir
        for file in self.out_files:
            if file is None:
                raise ValueError("out_files contained a None")
            # Raw tasks may not have a root_dir
            if root_dir := self.config.get("root_dir", None):
                if not file.startswith(root_dir):
                    raise ValueError(
                        f"Path error, output file {file} is not under root_dir {root_dir}"
                    )

        # Check for duplicate task outputs
        if self.config.command:
            for file in self.out_files:
                if file in app.all_out_files:
                    raise NameError(f"Multiple rules build {file}!")
                app.all_out_files.add(file)

        # Make sure our output directories exist
        if not app.flags.dry_run:
            for file in self.out_files:
                os.makedirs(path.dirname(file), exist_ok=True)

    # -----------------------------------------------------------------------------------------------

    def needs_rerun(self, force=False):
        """Checks if a task needs to be re-run, and returns a non-empty reason if so."""

        debug = self.config.get("debug", app.flags.debug)

        if force:
            return f"Files {self.out_files} forced to rebuild"
        if not self.in_files:
            return "Always rebuild a target with no inputs"
        if not self.out_files:
            return "Always rebuild a target with no outputs"

        # Check if any of our output files are missing.
        for file in self.out_files:
            if not path.exists(file):
                return f"Rebuilding because {file} is missing"

        # Check if any of our input files are newer than the output files.
        min_out = min(mtime(f) for f in self.out_files)

        if mtime(__file__) >= min_out:
            return "Rebuilding because hancho.py has changed"

        for file in self.in_files:
            if mtime(file) >= min_out:
                return f"Rebuilding because {file} has changed"

        for mod_filename in self._loaded_files:
            if mtime(mod_filename) >= min_out:
                return f"Rebuilding because {mod_filename} has changed"

        # Check all dependencies in the C dependencies file, if present.
        if (in_depfile := self.config.get("in_depfile", None)) and path.exists(
            in_depfile
        ):
            depformat = self.config.get("depformat", "gcc")
            if debug:
                log(f"Found C dependencies file {in_depfile}")
            with open(in_depfile, encoding="utf-8") as depfile:
                deplines = None
                if depformat == "msvc":
                    # MSVC /sourceDependencies
                    deplines = json.load(depfile)["Data"]["Includes"]
                elif depformat == "gcc":
                    # GCC -MMD
                    deplines = depfile.read().split()
                    deplines = [d for d in deplines[1:] if d != "\\"]
                else:
                    raise ValueError(f"Invalid dependency file format {depformat}")

                # The contents of the C dependencies file are RELATIVE TO THE WORKING DIRECTORY
                deplines = [path.join(self.config.task_dir, d) for d in deplines]
                for abs_file in deplines:
                    if mtime(abs_file) >= min_out:
                        return f"Rebuilding because {abs_file} has changed"

        # All checks passed; we don't need to rebuild this output.
        # Empty string = no reason to rebuild
        return ""

    # -----------------------------------------------------------------------------------------------

    async def run_command(self, command):
        """Runs a single command, either by calling it or running it in a subprocess."""

        verbose = self.config.get("verbose", app.flags.verbose)
        debug = self.config.get("debug", app.flags.debug)

        if verbose or debug:
            root_dir = self.config.get("root_dir", "/")
            log(color(128, 128, 255), end="")
            if app.flags.dry_run:
                log("(DRY RUN) ", end="")
            log(f"{rel_path(self.config.task_dir, root_dir)}$ ", end="")
            log(color(), end="")
            log(command)

        # Dry runs get early-out'ed before we do anything.
        if app.flags.dry_run:
            return

        # Custom commands just get called and then early-out'ed.
        if callable(command):
            app.pushdir(self.config.task_dir)
            command(self)
            app.popdir()
            self._returncode = 0
            return

        # Non-string non-callable commands are not valid
        if not isinstance(command, str):
            raise ValueError(f"Don't know what to do with {command}")

        # Create the subprocess via asyncio and then await the result.
        if debug:
            log(f"Task {hex(id(self))} subprocess start '{command}'")

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=self.config.task_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        (stdout_data, stderr_data) = await proc.communicate()

        if debug:
            log(f"Task {hex(id(self))} subprocess done '{command}'")

        self._stdout = stdout_data.decode()
        self._stderr = stderr_data.decode()
        self._returncode = proc.returncode

        # We need a better way to handle "should fail" so we don't constantly keep rerunning
        # intentionally-failing tests every build
        command_pass = (self._returncode == 0) != self.config.get("should_fail", False)

        if not command_pass:
            message = f"Command exited with return code {self._returncode}\n"
            if self._stdout:
                message += "Stdout:\n"
                message += self._stdout
            if self._stderr:
                message += "Stderr:\n"
                message += self._stderr
            raise ValueError(message)

        if debug or verbose:
            log(
                f"{color(128,255,196)}[{self._task_index}/{app.tasks_started}]{color()} Task passed - '{self.config.desc}'"
            )
            if self._stdout:
                log("Stdout:")
                log(self._stdout, end="")
            if self._stderr:
                log("Stderr:")
                log(self._stderr, end="")


####################################################################################################


class HanchoAPI(Utils):

    def __init__(self):
        self.config = Config()
        self.Config = Config
        self.Task = Task

    def __repr__(self):
        return Dumper(2).dump(self)

    def __contains__(self, key):
        return key in self.__dict__

    def __call__(self, arg1=None, /, *args, **kwargs):
        if callable(arg1):
            temp_config = Config(*args, **kwargs)
            return arg1(self, **temp_config)
        return Task(self.config, arg1, *args, **kwargs)

    def load(self, mod_path):
        mod_path = self.config.expand(mod_path)
        mod_path = normalize_path(mod_path)
        new_config = Config(
            self.config,
            mod_name=path.splitext(path.basename(mod_path))[0],
            mod_dir=path.dirname(mod_path),
            mod_path=mod_path,
            task_dir=path.dirname(mod_path),
            build_dir=Task.default_build_dir,
        )

        new_context = copy.deepcopy(self)
        new_context.config = new_config
        return new_context.load_module()

    def repo(self, mod_path):
        mod_path = self.config.expand(mod_path)
        mod_path = normalize_path(mod_path)
        new_config = Config(
            self.config,
            repo_name=path.basename(path.dirname(mod_path)),
            repo_dir=path.dirname(mod_path),
            mod_name=path.splitext(path.basename(mod_path))[0],
            mod_dir=path.dirname(mod_path),
            mod_path=mod_path,
            task_dir=path.dirname(mod_path),
            build_dir=Task.default_build_dir,
        )

        new_context = copy.deepcopy(self)
        new_context.config = new_config
        return new_context.load_module()

    def root(self, mod_path):
        mod_path = self.config.expand(mod_path)
        mod_path = normalize_path(mod_path)
        new_config = Config(
            self.config,
            root_dir=path.dirname(mod_path),
            root_path=mod_path,

            # Do we want new roots to use build/repo_name/...? Seems like no.
            #repo_name=path.basename(path.dirname(mod_path)),
            repo_name="",

            repo_dir=path.dirname(mod_path),
            build_root=Task.default_build_root,
            build_tag=Task.default_build_tag,
            mod_name=path.splitext(path.basename(mod_path))[0],
            mod_dir=path.dirname(mod_path),
            mod_path=mod_path,
        )

        new_context = copy.deepcopy(self)
        new_context.config = new_config
        return new_context.load_module()

    def load_module(self):
        if len(app.dirstack) == 1 or app.flags.verbose or app.flags.debug:
            log(("┃ " * (len(app.dirstack) - 1)), end="")
            log(color(128, 255, 128) + f"Loading {self.config.mod_path}" + color())

        app.loaded_files.append(self.config.mod_path)

        # We're using compile() and FunctionType()() here beause exec() doesn't preserve source
        # code for debugging.
        file = open(self.config.mod_path, encoding="utf-8")
        source = file.read()
        code = compile(source, self.config.mod_path, "exec", dont_inherit=True)

        # We must chdir()s into the .hancho file directory before running it so that
        # glob() can resolve files relative to the .hancho file itself. We are _not_ in an async
        # context here so there should be no other threads trying to change cwd.
        app.pushdir(path.dirname(self.config.mod_path))
        temp_globals = {"hancho": self, "__builtins__": builtins}

        # Pylint is just wrong here
        # pylint: disable=not-callable
        types.FunctionType(code, temp_globals)()
        app.popdir()

        # Module loaded, turn the module's globals into a Config that doesn't include __builtins__,
        # hancho, and imports so we don't have files that end up transitively containing the
        # universe
        new_module = Config()
        for key, val in temp_globals.items():
            if key.startswith("_") or key == "hancho" or isinstance(val, type(sys)):
                continue
            new_module[key] = val
        return new_module


####################################################################################################


class JobPool:
    def __init__(self):
        self.jobs_available = os.cpu_count()
        self.jobs_lock = asyncio.Condition()
        self.job_slots = [None] * self.jobs_available

    def reset(self, job_count):
        self.jobs_available = job_count
        self.job_slots = [None] * self.jobs_available

    ########################################

    async def acquire_jobs(self, count, token):
        """Waits until 'count' jobs are available and then removes them from the job pool."""

        if count > app.flags.jobs:
            raise ValueError(f"Need {count} jobs, but pool is {app.flags.jobs}.")

        await self.jobs_lock.acquire()
        await self.jobs_lock.wait_for(lambda: self.jobs_available >= count)

        slots_remaining = count
        for i, val in enumerate(self.job_slots):
            if val is None and slots_remaining:
                self.job_slots[i] = token
                slots_remaining -= 1

        self.jobs_available -= count
        self.jobs_lock.release()

    ########################################
    # NOTE: The notify_all here is required because we don't know in advance which tasks will
    # be capable of running after we return jobs to the pool. HOWEVER, this also creates an
    # O(N^2) slowdown when we have a very large number of pending tasks (>1000) due to the
    # "Thundering Herd" problem - all tasks will wake up, only a few will acquire jobs, the
    # rest will go back to sleep again, this will repeat for every call to release_jobs().

    async def release_jobs(self, count, token):
        """Returns 'count' jobs back to the job pool."""

        await self.jobs_lock.acquire()
        self.jobs_available += count

        slots_remaining = count
        for i, val in enumerate(self.job_slots):
            if val == token:
                self.job_slots[i] = None
                slots_remaining -= 1

        self.jobs_lock.notify_all()
        self.jobs_lock.release()


####################################################################################################


class App:

    def __init__(self):
        self.flags = None
        self.extra_flags = None
        self.target_regex = None

        self.root_context = None
        self.loaded_files = []
        self.dirstack = [os.getcwd()]

        self.all_out_files = set()

        self.mtime_calls = 0
        self.line_dirty = False
        self.expand_depth = 0
        self.shuffle = False

        self.tasks_started = 0
        self.tasks_running = 0
        self.tasks_finished = 0
        self.tasks_failed = 0
        self.tasks_skipped = 0
        self.tasks_cancelled = 0
        self.tasks_broken = 0

        self.all_tasks = []
        self.queued_tasks = []
        self.started_tasks = []
        self.finished_tasks = []
        self.log = ""

        self.job_pool = JobPool()
        self.parse_flags([])

    def reset(self):
        self.__init__()  # pylint: disable=unnecessary-dunder-call

    ########################################

    def parse_flags(self, argv):
        assert listlike(argv)

        # pylint: disable=line-too-long
        # fmt: off
        parser = argparse.ArgumentParser()
        parser.add_argument("target",            default=None, nargs="?", type=str,   help="A regex that selects the targets to build. Defaults to all targets.")

        parser.add_argument("-f", "--root_file", default="build.hancho",  type=str,   help="The name of the .hancho file(s) to build")
        parser.add_argument("-C", "--root_dir",  default=os.getcwd(),     type=str,   help="Change directory before starting the build")

        parser.add_argument("-v", "--verbose",   default=False, action="store_true",  help="Print verbose build info")
        parser.add_argument("-d", "--debug",     default=False, action="store_true",  help="Print debugging information")
        parser.add_argument("--force",           default=False, action="store_true",  help="Force rebuild of everything")
        parser.add_argument("--trace",           default=False, action="store_true",  help="Trace all text expansion")

        parser.add_argument("-j", "--jobs",      default=os.cpu_count(),  type=int,   help="Run N jobs in parallel (default = cpu_count)")
        parser.add_argument("-q", "--quiet",     default=False, action="store_true",  help="Mute all output")
        parser.add_argument("-n", "--dry_run",   default=False, action="store_true",  help="Do not run commands")
        parser.add_argument("-s", "--shuffle",   default=False, action="store_true",  help="Shuffle task order to shake out dependency issues")
        parser.add_argument("--use_color",       default=False, action="store_true",  help="Use color in the console output")
        # fmt: on

        (flags, unrecognized) = parser.parse_known_args(argv)

        # Unrecognized command line parameters also become global config fields if they are
        # flag-like
        extra_flags = {}
        for span in unrecognized:
            if match := re.match(r"-+([^=\s]+)(?:=(\S+))?", span):
                key = match.group(1)
                val = match.group(2)
                val = maybe_as_number(val) if val is not None else True
                extra_flags[key] = val

        self.flags = flags
        self.extra_flags = extra_flags

    ########################################
    # Needs to be its own function, used by run_tests.py

    def create_root_context(self):

        root_file = self.flags.root_file
        root_dir = path.realpath(self.flags.root_dir)  # Root path must be absolute.
        root_path = path.join(root_dir, root_file)

        root_config = Config(
            root_dir=root_dir,
            root_path=root_path,
            repo_name="",
            repo_dir=root_dir,
            mod_name=path.splitext(root_file)[0],
            mod_dir=root_dir,
            mod_path=root_path,
            build_root=Task.default_build_root,
            build_tag=Task.default_build_tag,
        )

        # All the unrecognized flags get stuck on the root context.
        for key, val in self.extra_flags.items():
            setattr(root_config, key, val)

        root_context = HanchoAPI()
        root_context.config = root_config
        return root_context

    ########################################

    def main(self):
        app.root_context = self.create_root_context()

        if app.root_context.config.get("debug", None):
            log(f"root_context = {Dumper(2).dump(app.root_context)}")

        if not path.isfile(app.root_context.config.root_path):
            print(
                f"Could not find root Hancho file {app.root_context.config.root_path}!"
            )
            sys.exit(-1)

        assert path.isabs(app.root_context.config.root_dir)
        assert path.isdir(app.root_context.config.root_dir)
        assert path.isabs(app.root_context.config.root_path)
        assert path.isfile(app.root_context.config.root_path)

        os.chdir(app.root_context.config.root_dir)
        time_a = time.perf_counter()
        app.root_context.load_module()
        time_b = time.perf_counter()

        # if app.flags.debug or app.flags.verbose:
        log(f"Loading .hancho files took {time_b-time_a:.3f} seconds")

        time_a = time.perf_counter()

        if app.flags.target:
            app.target_regex = re.compile(app.flags.target)
            for task in app.all_tasks:
                queue_task = False
                task_name = None
                # This doesn't work because we haven't expanded output filenames yet
                # for out_file in flatten(task.out_files):
                #    if app.target_regex.search(out_file):
                #        queue_task = True
                #        task_name = out_file
                #        break
                if name := task.get("name", None):
                    if app.target_regex.search(task.get("name", None)):
                        queue_task = True
                        task_name = name
                if queue_task:
                    log(f"Queueing task for '{task_name}'")
                    task.queue()
        else:
            for task in app.all_tasks:
                # If no target was specified, we queue up all tasks from the root repo.
                root_dir = task.config.get("root_dir", None)
                repo_dir = task.config.get("repo_dir", None)
                if root_dir == repo_dir:
                    task.queue()

        time_b = time.perf_counter()

        # if app.flags.debug or app.flags.verbose:
        log(f"Queueing {len(app.queued_tasks)} tasks took {time_b-time_a:.3f} seconds")

        result = self.build()
        return result

    ########################################

    def pushdir(self, new_dir: str):
        new_dir = abs_path(new_dir, strict=True)
        self.dirstack.append(new_dir)
        os.chdir(new_dir)

    def popdir(self):
        self.dirstack.pop()
        os.chdir(self.dirstack[-1])

    ########################################

    def build(self):
        """Run tasks until we're done with all of them."""
        result = -1
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = asyncio.run(self.async_run_tasks())
        loop.close()
        return result

    def build_all(self):
        for task in self.all_tasks:
            task.queue()
        return self.build()

    ########################################

    async def async_run_tasks(self):
        # Run all tasks in the queue until we run out.

        self.job_pool.reset(self.flags.jobs)

        # Tasks can create other tasks, and we don't want to block waiting on a whole batch of
        # tasks to complete before queueing up more. Instead, we just keep queuing up any pending
        # tasks after awaiting each one. Because we're awaiting tasks in the order they were
        # created, this will effectively walk through all tasks in dependency order.

        time_a = time.perf_counter()

        while self.queued_tasks or self.started_tasks:
            if app.shuffle:
                log(f"Shufflin' {len(self.queued_tasks)} tasks")
                random.shuffle(self.queued_tasks)

            while self.queued_tasks:
                task = self.queued_tasks.pop(0)
                task.start()
                self.started_tasks.append(task)

            task = self.started_tasks.pop(0)
            try:
                await task.asyncio_task
            except BaseException:  # pylint: disable=broad-exception-caught
                log(color(255, 128, 0), end="")
                log(f"Task failed: {task.config.desc}")
                log(color(), end="")
                log(str(task.config))
                log_exception()
            self.finished_tasks.append(task)

        time_b = time.perf_counter()

        # if app.flags.debug or app.flags.verbose:
        log(f"Running {app.tasks_started} tasks took {time_b-time_a:.3f} seconds")

        # Done, print status info if needed
        if app.flags.debug or app.flags.verbose:
            log(f"tasks started:   {app.tasks_started}")
            log(f"tasks finished:  {app.tasks_finished}")
            log(f"tasks failed:    {app.tasks_failed}")
            log(f"tasks skipped:   {app.tasks_skipped}")
            log(f"tasks cancelled: {app.tasks_cancelled}")
            log(f"tasks broken:    {app.tasks_broken}")
            log(f"mtime calls:     {app.mtime_calls}")

        if self.tasks_failed or self.tasks_broken:
            log(f"hancho: {color(255, 128, 128)}BUILD FAILED{color()}")
        elif self.tasks_finished:
            log(f"hancho: {color(128, 255, 128)}BUILD PASSED{color()}")
        else:
            log(f"hancho: {color(128, 128, 255)}BUILD CLEAN{color()}")

        return -1 if self.tasks_failed or self.tasks_broken else 0


####################################################################################################
# Always create an App() object so we can use it for bookkeeping even if we loaded Hancho as a
# module instead of running it directly.

app = App()

####################################################################################################

if __name__ == "__main__":
    app.parse_flags(sys.argv[1:])
    sys.exit(app.main())
