# -*- coding: UTF-8 -*-
"""
This module provides Runner class to run behave feature files (or model elements).
"""

from __future__ import absolute_import, print_function, with_statement

import contextlib
import os.path
import sys
import warnings
import weakref

import six

from behave.api.runner import ITestRunner
from behave._types import ExceptionUtil
from behave.capture import CaptureController
from behave.exception import ConfigError
from behave.formatter._registry import make_formatters
from behave.runner_util import \
    collect_feature_locations, parse_features, \
    exec_file, load_step_modules, PathManager
from behave.step_registry import registry as the_step_registry
from enum import Enum

if six.PY2:
    # -- USE PYTHON3 BACKPORT: With unicode traceback support.
    import traceback2 as traceback
else:
    import traceback


class CleanupError(RuntimeError):
    pass


class ContextMaskWarning(UserWarning):
    """Raised if a context variable is being overwritten in some situations.

    If the variable was originally set by user code then this will be raised if
    *behave* overwrites the value.

    If the variable was originally set by *behave* then this will be raised if
    user code overwrites the value.
    """
    pass


class ContextMode(Enum):
    """Used to distinguish between the two usage modes while using the context:

    * BEHAVE: Indicates "behave" (internal) mode
    * USER: Indicates "user" mode (in steps, hooks, fixtures, ...)
    """
    BEHAVE = 1
    USER = 2


class Context(object):
    """Hold contextual information during the running of tests.

    This object is a place to store information related to the tests you're
    running. You may add arbitrary attributes to it of whatever value you need.

    During the running of your tests the object will have additional layers of
    namespace added and removed automatically. There is a "root" namespace and
    additional namespaces for features and scenarios.

    Certain names are used by *behave*; be wary of using them yourself as
    *behave* may overwrite the value you set. These names are:

    .. attribute:: feature

      This is set when we start testing a new feature and holds a
      :class:`~behave.model.Feature`. It will not be present outside of a
      feature (i.e. within the scope of the environment before_all and
      after_all).

    .. attribute:: scenario

      This is set when we start testing a new scenario (including the
      individual scenarios of a scenario outline) and holds a
      :class:`~behave.model.Scenario`. It will not be present outside of the
      scope of a scenario.

    .. attribute:: tags

      The current set of active tags (as a Python set containing instances of
      :class:`~behave.model.Tag` which are basically just glorified strings)
      combined from the feature and scenario. This attribute will not be
      present outside of a feature scope.

    .. attribute:: aborted

      This is set to true in the root namespace when the user aborts a test run
      (:exc:`KeyboardInterrupt` exception). Initially: False.

    .. attribute:: failed

      This is set to true in the root namespace as soon as a step fails.
      Initially: False.

    .. attribute:: table

      This is set at the step level and holds any :class:`~behave.model.Table`
      associated with the step.

    .. attribute:: text

      This is set at the step level and holds any multiline text associated
      with the step.

    .. attribute:: config

      The configuration of *behave* as determined by configuration files and
      command-line options. The attributes of this object are the same as the
      `configuration file section names`_.

    .. attribute:: active_outline

      This is set for each scenario in a scenario outline and references the
      :class:`~behave.model.Row` that is active for the current scenario. It is
      present mostly for debugging, but may be useful otherwise.

    .. attribute:: captured

        If any output capture is enabled, provides access to a
        :class:`~behave.capture.Captured` object that contains a snapshot
        of all captured data (stdout/stderr/log).

        .. versionadded:: 1.3.0

    A :class:`behave.runner.ContextMaskWarning` warning will be raised if user
    code attempts to overwrite one of these variables, or if *behave* itself
    tries to overwrite a user-set variable.

    You may use the "in" operator to test whether a certain value has been set
    on the context, for example::

        "feature" in context

    checks whether there is a "feature" value in the context.

    Values may be deleted from the context using "del" but only at the level
    they are set. You can't delete a value set by a feature at a scenario level
    but you can delete a value set for a scenario in that scenario.

    .. _`configuration file section names`: behave.html#configuration-files
    """
    # pylint: disable=too-many-instance-attributes
    LAYER_NAMES = ["testrun", "feature", "rule", "scenario"]
    FAIL_ON_CLEANUP_ERRORS = True

    def __init__(self, runner):
        self._runner = weakref.proxy(runner)
        self._config = runner.config
        root_data = self._root = {
            "aborted": False,
            "failed": False,
            "config": self._config,
            "active_outline": None,
            "cleanup_errors": 0,
            "@cleanups": [],    # -- REQUIRED-BY: before_all() hook
            "@layer": "testrun",
        }
        self._stack = [root_data]
        self._record = {}
        self._origin = {}
        self._mode = ContextMode.BEHAVE

        # -- MODEL ENTITY REFERENCES/SUPPORT:
        # DISABLED: self.rule = None
        # DISABLED: self.scenario = None
        self.feature = None
        self.text = None
        self.table = None

        # -- RUNTIME SUPPORT:
        # DISABLED: self.stdout_capture = None
        # DISABLED: self.stderr_capture = None
        # DISABLED: self.log_capture = None
        self.fail_on_cleanup_errors = self.FAIL_ON_CLEANUP_ERRORS

    def abort(self, reason=None):
        """Abort the test run.

        This sets the :attr:`aborted` attribute to true.
        Any test runner evaluates this attribute to abort a test run.

        .. versionadded:: 1.2.7
        """
        self._set_root_attribute("aborted", True)

    @staticmethod
    def ignore_cleanup_error(context, cleanup_func, exception):
        pass

    @staticmethod
    def print_cleanup_error(context, cleanup_func, exception):
        cleanup_func_name = getattr(cleanup_func, "__name__", None)
        if not cleanup_func_name:
            cleanup_func_name = "%r" % cleanup_func
        print(u"CLEANUP-ERROR in %s: %s: %s" %
              (cleanup_func_name, exception.__class__.__name__, exception))
        traceback.print_exc(file=sys.stdout)
        # MAYBE: context._dump(pretty=True, prefix="Context: ")
        # -- MARK: testrun as FAILED
        # context._set_root_attribute("failed", True)

    def _do_cleanups(self):
        """Execute optional cleanup functions when stack frame is popped.
        A user can add a user-specified handler for cleanup errors.

        .. code-block:: python

            # -- FILE: features/environment.py
            def cleanup_database(database):
                pass

            def handle_cleanup_error(context, cleanup_func, exception):
                pass

            def before_all(context):
                context.on_cleanup_error = handle_cleanup_error
                context.add_cleanup(cleanup_database, the_database)
        """
        # -- BEST-EFFORT ALGORITHM: Tries to perform all cleanups.
        assert self._stack, "REQUIRE: Non-empty stack"
        current_layer = self._stack[0]
        cleanup_funcs = current_layer.get("@cleanups", [])
        on_cleanup_error = getattr(self, "on_cleanup_error",
                                   self.print_cleanup_error)
        context = self
        cleanup_errors = []
        for cleanup_func in reversed(cleanup_funcs):
            try:
                cleanup_func()
            except Exception as e: # pylint: disable=broad-except
                # pylint: disable=protected-access
                context._root["cleanup_errors"] += 1
                cleanup_errors.append(sys.exc_info())
                on_cleanup_error(context, cleanup_func, e)

        if self.fail_on_cleanup_errors and cleanup_errors:
            first_cleanup_erro_info = cleanup_errors[0]
            del cleanup_errors  # -- ENSURE: Release other exception frames.
            six.reraise(*first_cleanup_erro_info)

    def _push(self, layer=None):
        """Push a new layer on the context stack.
        HINT: Use layer values: "testrun", "feature", "rule, "scenario".

        :param layer:   Layer name to use (or None).
        """
        initial_data = {"@cleanups": []}
        if layer:
            initial_data["@layer"] = layer
        self._stack.insert(0, initial_data)

    def _pop(self):
        """Pop the current layer from the context stack.
        Performs any pending cleanups, registered for this layer.
        """
        try:
            self._do_cleanups()
        finally:
            # -- ENSURE: Layer is removed even if cleanup-errors occur.
            self._stack.pop(0)

    def _use_with_behave_mode(self):
        """Provides a context manager for using the context in BEHAVE mode."""
        return use_context_with_mode(self, ContextMode.BEHAVE)

    def use_with_user_mode(self):
        """Provides a context manager for using the context in USER mode."""
        return use_context_with_mode(self, ContextMode.USER)

    def user_mode(self):
        warnings.warn("Use 'use_with_user_mode()' instead",
                      PendingDeprecationWarning, stacklevel=2)
        return self.use_with_user_mode()

    def _set_root_attribute(self, attr, value):
        for frame in self.__dict__["_stack"]:
            if frame is self.__dict__["_root"]:
                continue
            if attr in frame:
                record = self.__dict__["_record"][attr]
                params = {
                    "attr": attr,
                    "filename": record[0],
                    "line": record[1],
                    "function": record[3],
                }
                self._emit_warning(attr, params)

        self.__dict__["_root"][attr] = value
        if attr not in self._origin:
            self._origin[attr] = self._mode

    def _emit_warning(self, attr, params):
        msg = ""
        if self._mode is ContextMode.BEHAVE and self._origin[attr] is not ContextMode.BEHAVE:
            msg = "behave runner is masking context attribute '%(attr)s' " \
                  "originally set in %(function)s (%(filename)s:%(line)s)"
        elif self._mode is ContextMode.USER:
            if self._origin[attr] is not ContextMode.USER:
                msg = "user code is masking context attribute '%(attr)s' " \
                      "originally set by behave"
            elif self._config.verbose:
                msg = "user code is masking context attribute " \
                    "'%(attr)s'; see the tutorial for what this means"
        if msg:
            msg = msg % params
            warnings.warn(msg, ContextMaskWarning, stacklevel=3)

    def _dump(self, pretty=False, prefix="  "):
        for level, frame in enumerate(self._stack):
            print("%sLevel %d" % (prefix, level))
            if pretty:
                for name in sorted(frame.keys()):
                    value = frame[name]
                    print("%s  %-15s = %r" % (prefix, name, value))
            else:
                print(prefix + repr(frame))

    def __getattr__(self, attr):
        if attr[0] == "_":
            try:
                return self.__dict__[attr]
            except KeyError:
                raise AttributeError(attr)

        for frame in self._stack:
            if attr in frame:
                return frame[attr]
        msg = "'{0}' object has no attribute '{1}'"
        msg = msg.format(self.__class__.__name__, attr)
        raise AttributeError(msg)

    def __setattr__(self, attr, value):
        if attr[0] == "_":
            self.__dict__[attr] = value
            return

        for frame in self._stack[1:]:
            if attr in frame:
                record = self._record[attr]
                params = {
                    "attr": attr,
                    "filename": record[0],
                    "line": record[1],
                    "function": record[3],
                }
                self._emit_warning(attr, params)

        stack_limit = 2
        if six.PY2:
            stack_limit += 1     # Due to traceback2 usage.
        stack_frame = traceback.extract_stack(limit=stack_limit)[0]
        self._record[attr] = stack_frame
        frame = self._stack[0]
        frame[attr] = value
        if attr not in self._origin:
            self._origin[attr] = self._mode

    def __delattr__(self, attr):
        frame = self._stack[0]
        if attr in frame:
            del frame[attr]
            del self._record[attr]
        else:
            msg = "'{0}' object has no attribute '{1}' at the current level"
            msg = msg.format(self.__class__.__name__, attr)
            raise AttributeError(msg)

    def __contains__(self, attr):
        if attr[0] == "_":
            return attr in self.__dict__
        for frame in self._stack:
            if attr in frame:
                return True
        return False

    def execute_steps(self, steps_text):
        """The steps identified in the "steps" text string will be parsed and
        executed in turn just as though they were defined in a feature file.

        If the execute_steps call fails (either through error or failure
        assertion) then the step invoking it will need to catch the resulting
        exceptions.

        :param steps_text:  Text with the Gherkin steps to execute (as string).
        :returns: True, if the steps executed successfully.
        :raises: AssertionError, if a step failure occurs.
        :raises: ValueError, if invoked without a feature context.
        """
        assert isinstance(steps_text, six.text_type), "Steps must be unicode."
        if not self.feature:
            raise ValueError("execute_steps() called outside of feature")

        # -- PREPARE: Save original context data for current step.
        # Needed if step definition that called this method uses .table/.text
        original_table = getattr(self, "table", None)
        original_text = getattr(self, "text", None)

        self.feature.parser.variant = "steps"
        steps = self.feature.parser.parse_steps(steps_text)
        with self._use_with_behave_mode():
            for step in steps:
                passed = step.run(self._runner, quiet=True, capture=False)
                if not passed:
                    # -- ISSUE #96: Provide more substep info to diagnose problem.
                    step_line = u"%s %s" % (step.keyword, step.name)
                    message = "%s SUB-STEP: %s" % \
                              (step.status.name.upper(), step_line)
                    if step.error_message:
                        message += "\nSubstep info: %s\n" % step.error_message
                        message += u"Traceback (of failed substep):\n"
                        message += u"".join(traceback.format_tb(step.exc_traceback))
                    # message += u"\nTraceback (of context.execute_steps()):"
                    assert False, message

            # -- FINALLY: Restore original context data for current step.
            self.table = original_table
            self.text = original_text
        return True

    def _select_stack_frame_by_layer(self, layer):
        """Select context stack frame by layer name.

        :param layer:   Layer name (as string).
        :return: Selected frame object (if any)
        :raises: LookupError, if layer was not found.
        """
        for frame in self._stack:
            frame_layer = frame.get("@layer", None)
            if layer == frame_layer:
                return frame
        # -- OOPS, NOT FOUND:
        raise LookupError("Context.stack: layer=%s not found" % layer)

    def add_cleanup(self, cleanup_func, *args, **kwargs):
        """Adds a cleanup function that is called when :meth:`Context._pop()`
        is called. This is intended for user-cleanups.

        :param cleanup_func:    Callable function
        :param args:            Args for cleanup_func() call (optional).
        :param kwargs:          Kwargs for cleanup_func() call (optional).

        .. note:: RESERVED :obj:`layer` : optional-string

            The keyword argument ``layer="LAYER_NAME"`` can to be used to
            assign the :obj:`cleanup_func` to specific a layer on the context stack
            (instead of the current layer).

            Known layer names are: "testrun", "feature", "rule", "scenario"

        .. seealso:: :attr:`.Context.LAYER_NAMES`
        """
        # MAYBE:
        assert callable(cleanup_func), "REQUIRES: callable(cleanup_func)"
        assert self._stack
        layer_name = kwargs.pop("layer", None)
        if args or kwargs:
            def internal_cleanup_func():
                cleanup_func(*args, **kwargs)
        else:
            internal_cleanup_func = cleanup_func

        current_frame = self._stack[0]
        if layer_name:
            current_frame = self._select_stack_frame_by_layer(layer_name)
        if cleanup_func not in current_frame["@cleanups"]:
            # -- AVOID DUPLICATES:
            current_frame["@cleanups"].append(internal_cleanup_func)

    @property
    def captured(self):
        return self._runner.captured

    def attach(self, mime_type, data):
        """Embeds data (e.g. a screenshot) in reports for all
        formatters that support it, such as the JSON formatter.

        :param mime_type:       MIME type of the binary data.
        :param data:            Bytes-like object to embed.
        """
        is_compatible = lambda f: hasattr(f, "embedding")
        for formatter in filter(is_compatible, self._runner.formatters):
            formatter.embedding(mime_type, data)


@contextlib.contextmanager
def use_context_with_mode(context, mode):
    """Switch context to ContextMode.BEHAVE or ContextMode.USER mode.
    Provides a context manager for switching between the two context modes.

    .. sourcecode:: python

        context = Context()
        with use_context_with_mode(context, ContextMode.BEHAVE):
            ...     # Do something
        # -- POSTCONDITION: Original context._mode is restored.

    :param context:  Context object to use.
    :param mode:     Mode to apply to context object.
    """
    # pylint: disable=protected-access
    assert mode in (ContextMode.BEHAVE, ContextMode.USER)
    current_mode = context._mode
    try:
        context._mode = mode
        yield
    finally:
        # -- RESTORE: Initial current_mode
        #    Even if an AssertionError/Exception is raised.
        context._mode = current_mode


@contextlib.contextmanager
def scoped_context_layer(context, layer=None):
    """Provides context manager for context layer (push/do-something/pop cycle).

    .. code-block::

        with scoped_context_layer(context):
            the_fixture = use_fixture(foo, context, name="foo_42")
    """
    # pylint: disable=protected-access
    try:
        context._push(layer)
        yield context
    finally:
        context._pop()


def path_getrootdir(path):
    """
    Extract rootdir from path in a platform independent way.

    POSIX-PATH EXAMPLE:
        rootdir = path_getrootdir("/foo/bar/one.feature")
        assert rootdir == "/"

    WINDOWS-PATH EXAMPLE:
        rootdir = path_getrootdir("D:\\foo\\bar\\one.feature")
        assert rootdir == r"D:\"
    """
    drive, _ = os.path.splitdrive(path)
    if drive:
        # -- WINDOWS:
        return drive + os.path.sep
    # -- POSIX:
    return os.path.sep


class ModelRunner(object):
    """Test runner for a behave model (features).
    Provides the core functionality of a test runner and
    the functional API needed by model elements.

    .. attribute:: aborted

          This is set to true when the user aborts a test run
          (:exc:`KeyboardInterrupt` exception). Initially: False.
          Stored as derived attribute in :attr:`Context.aborted`.

    .. attribute:: captured

        If any output capture is enabled, provides access to a
        :class:`~behave.capture.Captured` object that contains a snapshot
        of all captured data (stdout/stderr/log).

        .. versionadded:: 1.3.0
    """
    # pylint: disable=too-many-instance-attributes

    def __init__(self, config, features=None, step_registry=None):
        self.config = config
        self.features = features or []
        self.hooks = {}
        self.formatters = []
        self._undefined_steps = []
        self.step_registry = step_registry
        self.capture_controller = CaptureController(config)

        self.context = None
        self.feature = None
        self.hook_failures = 0

    @property
    def undefined_steps(self):
        return self._undefined_steps

    @property
    def aborted(self):
        """Indicates that test run is aborted by the user or system."""
        if self.context:
            return self.context.aborted
        # -- OTHERWISE
        return False

    @aborted.setter
    def aborted(self, value):
        """Mark the test run as aborted."""
        # pylint: disable=protected-access
        assert self.context, "REQUIRE: context, but context=%r" % self.context
        if self.context:
            self.context._set_root_attribute("aborted", bool(value))

    # DISABLED: aborted = property(_get_aborted, _set_aborted, doc="...")

    def abort(self, reason=None):
        """Abort the test run.

        .. versionadded:: 1.2.7
        """
        if self.context is None:
            return  # -- GRACEFULLY IGNORED.

        # -- NORMAL CASE:
        # SIMILAR TO: self.aborted = True
        self.context.abort(reason=reason)

    def run_hook(self, name, context, *args):
        if not self.config.dry_run and (name in self.hooks):
            try:
                with context.use_with_user_mode():
                    self.hooks[name](context, *args)
            # except KeyboardInterrupt:
            #     self.abort(reason="KeyboardInterrupt")
            #     if name not in ("before_all", "after_all"):
            #         raise
            except Exception as e:  # pylint: disable=broad-except
                # -- HANDLE HOOK ERRORS:
                use_traceback = False
                if self.config.verbose:
                    use_traceback = True
                    ExceptionUtil.set_traceback(e)
                extra = u""
                if "tag" in name:
                    extra = "(tag=%s)" % args[0]

                error_text = ExceptionUtil.describe(e, use_traceback).rstrip()
                error_message = u"HOOK-ERROR in %s%s: %s" % (name, extra, error_text)
                print(error_message)
                self.hook_failures += 1
                if "tag" in name:
                    # -- SCENARIO or FEATURE
                    statement = getattr(context, "scenario", context.feature)
                elif "all" in name:
                    # -- ABORT EXECUTION: For before_all/after_all
                    self.abort(reason="HOOK-ERROR in hook=%s" % name)
                    statement = None
                else:
                    # -- CASE: feature, scenario, step
                    statement = args[0]

                if statement:
                    # -- CASE: feature, scenario, step
                    statement.hook_failed = True
                    if statement.error_message:
                        # -- NOTE: One exception/failure is already stored.
                        #    Append only error message.
                        statement.error_message += u"\n"+ error_message
                    else:
                        # -- FIRST EXCEPTION/FAILURE:
                        statement.store_exception_context(e)
                        statement.error_message = error_message

    def setup_capture(self):
        if not self.context:
            self.context = Context(self)
        self.capture_controller.setup_capture(self.context)

    def start_capture(self):
        self.capture_controller.start_capture()

    def stop_capture(self):
        self.capture_controller.stop_capture()

    def teardown_capture(self):
        self.capture_controller.teardown_capture()

    @property
    def captured(self):
        """Return the current state of the captured output/logging
        (as captured object).
        """
        return self.capture_controller.captured

    def run_model(self, features=None):
        # pylint: disable=too-many-branches
        if not self.context:
            self.context = Context(self)
        if self.step_registry is None:
            self.step_registry = the_step_registry
        if features is None:
            features = self.features

        # -- ENSURE: context.execute_steps() works in weird cases (hooks, ...)
        context = self.context
        self.hook_failures = 0
        self.setup_capture()
        self.run_hook("before_all", context)

        run_feature = not self.aborted
        failed_count = 0
        undefined_steps_initial_size = len(self.undefined_steps)
        for feature in features:
            if run_feature:
                try:
                    self.feature = feature
                    for formatter in self.formatters:
                        formatter.uri(feature.filename)

                    failed = feature.run(self)
                    if failed:
                        failed_count += 1
                        if self.config.stop or self.aborted:
                            # -- FAIL-EARLY: After first failure.
                            run_feature = False
                except KeyboardInterrupt:
                    self.abort(reason="KeyboardInterrupt")
                    failed_count += 1
                    run_feature = False

            # -- ALWAYS: Report run/not-run feature to reporters.
            # REQUIRED-FOR: Summary to keep track of untested features.
            for reporter in self.config.reporters:
                reporter.feature(feature)

        # -- AFTER-ALL:
        # pylint: disable=protected-access, broad-except
        cleanups_failed = False
        self.run_hook("after_all", self.context)
        try:
            self.context._do_cleanups()   # Without dropping the last context layer.
        except Exception:
            cleanups_failed = True

        if self.aborted:
            print("\nABORTED: By user.")
        for formatter in self.formatters:
            formatter.close()
        for reporter in self.config.reporters:
            reporter.end()

        failed = ((failed_count > 0) or self.aborted or (self.hook_failures > 0)
                  or (len(self.undefined_steps) > undefined_steps_initial_size)
                  or cleanups_failed)
                  # XXX-MAYBE: or context.failed)
        return failed

    def run(self):
        """
        Implements the run method by running the model.
        """
        self.context = Context(self)
        return self.run_model()


class Runner(ModelRunner):
    """Standard test runner for behave:

      * setup paths
      * loads environment hooks
      * loads step definitions
      * select feature files, parses them and creates model (elements)
    """

    def __init__(self, config):
        super(Runner, self).__init__(config)
        self.path_manager = PathManager()
        self.base_dir = None

    def setup_paths(self):
        # pylint: disable=too-many-branches, too-many-statements
        if self.config.paths:
            if self.config.verbose:
                print("Supplied path:", \
                      ", ".join('"%s"' % path for path in self.config.paths))
            first_path = self.config.paths[0]
            if hasattr(first_path, "filename"):
                # -- BETTER: isinstance(first_path, FileLocation):
                first_path = first_path.filename
            base_dir = first_path
            if base_dir.startswith("@"):
                # -- USE: behave @features.txt
                base_dir = base_dir[1:]
                file_locations = self.feature_locations()
                if file_locations:
                    base_dir = os.path.dirname(file_locations[0].filename)
            base_dir = os.path.abspath(base_dir)

            # supplied path might be to a feature file
            if os.path.isfile(base_dir):
                if self.config.verbose:
                    print("Primary path is to a file so using its directory")
                base_dir = os.path.dirname(base_dir)
        else:
            if self.config.verbose:
                print('Using default path "./features"')
            base_dir = os.path.abspath("features")

        # Get the root. This is not guaranteed to be "/" because Windows.
        root_dir = path_getrootdir(base_dir)
        new_base_dir = base_dir
        steps_dir = self.config.steps_dir
        environment_file = self.config.environment_file

        while True:
            if self.config.verbose:
                print("Trying base directory:", new_base_dir)

            if os.path.isdir(os.path.join(new_base_dir, steps_dir)):
                break
            if os.path.isfile(os.path.join(new_base_dir, environment_file)):
                break
            if new_base_dir == root_dir:
                break

            new_base_dir = os.path.dirname(new_base_dir)

        if new_base_dir == root_dir:
            if self.config.verbose:
                if not self.config.paths:
                    print('ERROR: Could not find "%s" directory. '\
                          'Please specify where to find your features.' % \
                                steps_dir)
                else:
                    print('ERROR: Could not find "%s" directory in your '\
                        'specified path "%s"' % (steps_dir, base_dir))

            message = 'No %s directory in %r' % (steps_dir, base_dir)
            raise ConfigError(message)

        base_dir = new_base_dir
        self.config.base_dir = base_dir

        for dirpath, dirnames, filenames in os.walk(base_dir, followlinks=True):
            if [fn for fn in filenames if fn.endswith(".feature")]:
                break
        else:
            if self.config.verbose:
                if not self.config.paths:
                    print('ERROR: Could not find any "<name>.feature" files. '\
                        'Please specify where to find your features.')
                else:
                    print('ERROR: Could not find any "<name>.feature" files '\
                        'in your specified path "%s"' % base_dir)
            raise ConfigError('No feature files in %r' % base_dir)

        self.base_dir = base_dir
        self.path_manager.add(base_dir)
        if not self.config.paths:
            self.config.paths = [base_dir]

        if base_dir != os.getcwd():
            self.path_manager.add(os.getcwd())

    def before_all_default_hook(self, context):
        """
        Default implementation for :func:`before_all()` hook.
        Setup the logging subsystem based on the configuration data.
        """
        # pylint: disable=no-self-use
        context.config.setup_logging()

    def load_hooks(self, filename=None):
        filename = filename or self.config.environment_file
        hooks_path = os.path.join(self.base_dir, filename)
        if os.path.exists(hooks_path):
            exec_file(hooks_path, self.hooks)

        if "before_all" not in self.hooks:
            self.hooks["before_all"] = self.before_all_default_hook

    def load_step_definitions(self, extra_step_paths=None):
        if extra_step_paths is None:
            extra_step_paths = []
        # -- Allow steps to import other stuff from the steps dir
        # NOTE: Default matcher can be overridden in "environment.py" hook.
        steps_dir = os.path.join(self.base_dir, self.config.steps_dir)
        step_paths = [steps_dir] + list(extra_step_paths)
        load_step_modules(step_paths)

    def feature_locations(self):
        return collect_feature_locations(self.config.paths)

    def run(self):
        with self.path_manager:
            self.setup_paths()
            return self.run_with_paths()

    def run_with_paths(self):
        self.context = Context(self)
        self.load_hooks()
        self.load_step_definitions()

        # -- ENSURE: context.execute_steps() works in weird cases (hooks, ...)
        # self.setup_capture()
        # self.run_hook("before_all", self.context)

        # -- STEP: Parse all feature files (by using their file location).
        feature_locations = [filename for filename in self.feature_locations()
                             if not self.config.exclude(filename)]
        features = parse_features(feature_locations, language=self.config.lang)
        self.features.extend(features)

        # -- STEP: Run all features.
        stream_openers = self.config.outputs
        self.formatters = make_formatters(self.config, stream_openers)
        return self.run_model()


# -----------------------------------------------------------------------------
# REGISTER RUNNER-CLASSES:
# -----------------------------------------------------------------------------
ITestRunner.register(ModelRunner)
ITestRunner.register(Runner)
