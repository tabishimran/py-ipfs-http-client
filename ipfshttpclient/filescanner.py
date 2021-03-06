import abc
import collections.abc
import enum
import fnmatch
import os
import re
import sys
import typing as ty

from . import utils


if sys.version_info >= (3, 7):  #PY37+
	if ty.TYPE_CHECKING:
		re_pattern_t = re.Pattern[ty.AnyStr]
	else:
		re_pattern_t = re.Pattern
else:  #PY36-
	re_pattern_t = type(re.compile(""))

if hasattr(ty, "Literal"):  #PY38+
	ty_Literal_True = ty.Literal[True]
else:  #PY37-
	ty_Literal_True = bool


if hasattr(enum, "auto"):  #PY36+
	enum_auto = enum.auto
else:  #PY35
	_counter = 0
	
	def enum_auto() -> int:
		global _counter
		_counter += 1
		return _counter


O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)  # type: int


HAVE_FWALK       = hasattr(os, "fwalk")  # type: bool
HAVE_FWALK_BYTES = HAVE_FWALK and sys.version_info >= (3, 7)  # type: bool


class Matcher(ty.Generic[ty.AnyStr], metaclass=abc.ABCMeta):
	"""Represents a type that can match on file paths and decide whether they
	should be included in some file scanning/adding operation"""
	__slots__ = ()
	
	@abc.abstractmethod
	def should_descend(self, path: ty.AnyStr) -> bool:
		r"""Decides whether the file scanner should descend into the given directory path
		
		Arguments
		---------
		path
			A directory path upholding the same guarantees as those
			mentioned in :meth:`should_store`
		"""
	
	@abc.abstractmethod
	def should_report(self, path: ty.AnyStr, *, is_dir: bool) -> bool:
		r"""Decides whether the file scanner should store the given file or directory
		
		Note that in this case “file” may refer to anything that is not a
		directory and not just regular files. If the settings of the file scanner
		do not permit it to follow symbolic links this may even include symbolic
		links pointing at directories.
		
		Arguments
		---------
		path
			The file or directory path to check – the argument's type depends on
			the type of the path originally passed to the file scanner and may
			either be :type:`bytes` or :type:`str`, but will usually be :type:`str`
			
			The given path is guaranteed to have the following additional properties:
			
			* It will be properly normalized: There won't be any empty (``…//…`),
			  single-dot (``…/./…``) or (``…/../…``) directory labels or leading
			  or trailing slashes.
			* Its path separator will match the one found in :var:`os.path.sep` –
			  that is: It will be \ on Windows and / everywhere else.
			* It will be relative to the file scanner's base directory.
		is_dir
			Whether the given path refers to a directory, see the above paragraph
			for what this means exactly
		"""


class DummyMatcher(Matcher[ty.AnyStr]):
	"""I want it all – I want it now…"""
	__slots__ = ()
	
	def should_descend(self, path: ty.AnyStr) -> ty_Literal_True:
		return True
	
	def should_report(self, path: ty.AnyStr, *, is_dir: bool) -> ty_Literal_True:
		return True


DUMMY_MATCHER = DummyMatcher()  # type: DummyMatcher


class GlobMatcher(Matcher[ty.AnyStr], ty.Generic[ty.AnyStr]):
	"""Matches files and directories according to the shell glob conventions
	
	For details on the syntax see the Python :py:mod:`glob` module that this
	class emulates. If your are accustomed the globing on real Unix shells
	make sure to also carefully study its limitations as these also apply here.
	Also not that this matcher always has recursion enabled and hence treats
	``**``-labels as special. Additionally the *period_special* parameter is
	provided that may be used to disable the special handling of “dot-files”
	(files whose name starts with a leading period).
	
	One important thing to keep in mind that this is a *matcher* and works
	entirely I/O-less. As such, trying to include any files or directories
	*outside* of the matching domain will *not* work. For instance, a pattern
	like ``../a`` or ``b/../c`` would never match anything as a conforming
	file scanner would never pass in such a path, the same applies to any notion
	of absolute paths. This matcher will try its best to normalize or reject
	such cases, but if you're wondering why your pattern just won't match while
	pasting it into a real shell works this may be why.
	"""
	__slots__ = ("period_special", "_sep", "_pat", "_dir_only")
	#period_special: bool
	#_sep: ty.AnyStr
	#_pat: ty.List[ty.Optional[re_pattern_t]]
	#_dir_only: bool
	
	def __init__(self, pat: ty.AnyStr, *, period_special: bool = True):
		"""
		Arguments
		---------
		pat
			The glob pattern to use for matching
		period_special
			Whether a leading period in file/directory names should be matchable by
			``*``, ``?`` and ``[…]`` – traditionally they are not, but many modern
			shells allow one to disable this behaviour
		"""
		self.period_special = period_special  # type: bool
		
		self._sep = utils.maybe_fsencode(os.path.sep, pat)  # type: ty.AnyStr
		dblstar = utils.maybe_fsencode("**", pat)  # type: ty.AnyStr
		dot = utils.maybe_fsencode(".", pat)  # type: ty.AnyStr
		pat_ndot = utils.maybe_fsencode(r"(?![.])", pat)  # type: ty.AnyStr
		
		# Normalize path separator
		if os.path.altsep:
			pat = pat.replace(utils.maybe_fsencode(os.path.altsep, pat), self._sep)
		
		# Sanity checks for stuff that will definitely NOT EVER match
		# (there is another one in the loop below)
		assert not os.path.isabs(pat), "Absolute matching patterns will never match"
		
		# Note the extra final slash for its effect of only matching directories
		#
		# (TBH, I find it hard to see how that is useful, but everybody does it
		#  and it keeps things consistent overall – something to only match files
		#  would be nice however.)
		self._dir_only = pat.endswith(self._sep)  # type: bool
		
		self._pat = []  # type: ty.List[ty.Optional[re_pattern_t]]
		for label in pat.split(self._sep):
			# Skip over useless path components
			if len(label) < 1 or label == dot:
				continue
			
			assert label != dot + dot, 'Matching patterns containing ".." will never match'
			
			if label == dblstar:
				self._pat.append(None)
			elif dblstar in label:
				raise NotImplementedError(
					"Using double-star (**) and other characters in the same glob "
					"path label ({0}) is not currently supported – please do file "
					"an issue if you need this!".format(os.fsdecode(label))
				)
			else:
				#re_expr: ty.AnyStr
				if not isinstance(label, bytes):
					re_expr = fnmatch.translate(label)
				else:
					re_expr = fnmatch.translate(label.decode("latin-1")).encode("latin-1")
				
				if period_special and not label.startswith(dot):
					re_expr = pat_ndot + re_expr
				self._pat.append(re.compile(re_expr))
	
	
	def should_descend(self, path: ty.AnyStr) -> bool:
		for idx, label in enumerate(path.split(self._sep)):
			# Always descend into any directory below a recursive pattern as we
			# cannot predict what we will later do a tail match on
			if self._pat[idx] is None:
				return True
			
			# Do not descend further if we reached the last label of the pattern
			# (unless the final pattern label is a recursive match, see above)
			#
			# This is independent of whether this *directory* will be included
			# or not.
			if idx == (len(self._pat) - 1):
				return False
			
			# Match the current pattern to decide whether to keep looking or not
			if not self._pat[idx].match(label):
				return False
		
		# The given path matched part of this pattern, so we should include this
		# directory to go further
		return True
	
	
	def should_report(self, path: ty.AnyStr, *, is_dir: bool) -> bool:
		# A final slash means “only match directories”
		if self._dir_only and not is_dir:
			return False
		
		labels = path.split(self._sep)  # type: ty.List[ty.AnyStr]
		return self._match(labels, idx_pat=0, idx_path=0, is_dir=is_dir)
	
	
	def _match(self, labels: ty.List[ty.AnyStr], *, idx_pat: int, idx_path: int,
	           is_dir: bool) -> bool:
		while idx_pat < len(self._pat) and self._pat[idx_pat] is not None:
			# Match initial labels before recursion
			if idx_path >= len(labels):
				# Pattern refers to something below this path, store it only if it
				# is a directory
				return is_dir
			elif not self._pat[idx_pat].match(labels[idx_path]):
				# Pattern did not match
				return False
			
			idx_pat += 1
			idx_path += 1
		
		dot = utils.maybe_fsencode(".", labels[0])  # type: ty.AnyStr
		
		# We reached the end of the matching labels or the start of recursion
		if idx_pat == len(self._pat):
			# End of matching labels – only include path if it was of the same
			# length or the previous pattern label was recursive
			if self._pat[idx_pat - 1] is None:
				return not self.period_special or not labels[idx_path].startswith(dot)
			else:
				return idx_path == len(labels)
		
		# Start of recursion – move to next label and recurse this method too
		#
		# If the path is then matched by our inferior self return success,
		# otherwise retry with the next path label until all labels have been
		# exhausted meaning that there was no match.
		idx_pat += 1
		while idx_path < len(labels):
			if self._match(labels, idx_pat=idx_pat, idx_path=idx_path, is_dir=is_dir):
				return True
			
			# Do not add dot-files as part of recursive patterns by default
			if self.period_special and labels[idx_path].startswith(dot):
				break
			
			idx_path += 1
		
		# Nothing matched
		return False


class ReMatcher(Matcher[ty.AnyStr], ty.Generic[ty.AnyStr]):
	"""Matches files and directories using a regular expression pattern
	
	See the description of :meth:`Matcher.should_store` for the specifics on how
	the matching path is formatted, but note that there is one important
	difference: In order to allow the regular expression to distinguish between
	files and directories, all directories (if *is_dir* is ``True``) contain
	a trailing forward or backward slash (depending on the platform). If you
	don't care about the this information you may want to add ``[\\/]?`` to end
	of the pattern.
	
	Unlike glob patterns, regular expressions cannot be reliably analyzed for
	which directories the file paths they may or may not match are in. Because
	of this, this matcher will cause the file scanner **to recurse into any
	directory it encounters** possibly causing an unnecessarily slow-down during
	scanning even if only very few files end up being selected. If this causes
	problems for you *use non-recursive glob patterns instead* or implement your
	own matcher with a proper :meth:`Matcher.should_descend` method.
	"""
	__slots__ = ("_pat",)
	#_pat: re_pattern_t
	
	def __init__(self, pat: re_pattern_t):
		self._pat = re.compile(pat)  # type: re_pattern_t
	
	def should_descend(self, path: ty.AnyStr) -> bool:
		return True
	
	def should_report(self, path: ty.AnyStr, *, is_dir: bool) -> bool:
		suffix = utils.maybe_fsencode(os.path.sep, path) if is_dir else type(path)()  # type: ty.AnyStr
		return bool(self._pat.match(path + suffix))


class MetaMatcher(Matcher[ty.AnyStr], ty.Generic[ty.AnyStr]):
	"""Match files and directories by delegating to other matchers"""
	__slots__ = ("_children",)
	#_children: ty.List[Matcher[ty.AnyStr]]
	
	def __init__(self, children: ty.List[Matcher[ty.AnyStr]]):
		self._children = children  # type: ty.List[Matcher[ty.AnyStr]]
	
	def should_descend(self, path: ty.AnyStr) -> bool:
		return any(m.should_descend(path) for m in self._children)
	
	def should_report(self, path: ty.AnyStr, *, is_dir: bool) -> bool:
		return any(m.should_report(path, is_dir=is_dir) for m in self._children)


class NoRecusionAdapterMatcher(Matcher[ty.AnyStr], ty.Generic[ty.AnyStr]):
	"""Matcher adapter that will prevent any recusion
	
	Takes a subordinate matcher, but tells the scanner to never descend into any
	child directory and to never store files from such a directory. This is an
	effective way to prevent any non-top-level files from being emitted by the
	scanner and hence provides ``recursive=False`` semantics.
	"""
	__slots__ = ("_child",)
	#_child: Matcher[ty.AnyStr]
	
	def __init__(self, child: Matcher[ty.AnyStr]):
		self._child = child  # type: Matcher[ty.AnyStr]
		super().__init__()
	
	def should_descend(self, path: ty.AnyStr) -> bool:
		return False
	
	def should_report(self, path: ty.AnyStr, *, is_dir: bool) -> bool:
		return os.path.sep not in path and self._child.should_report(path, is_dir=is_dir)


_match_spec_t = ty.Union[ty.AnyStr, re_pattern_t, Matcher[ty.AnyStr]]
match_spec_t = ty.Optional[ty.Union[
	ty.Iterable[_match_spec_t[ty.AnyStr]],
	_match_spec_t[ty.AnyStr]
]]


def matcher_from_spec(spec: match_spec_t[ty.AnyStr], *,
                      period_special: bool = True,
                      recursive: bool = True) -> Matcher[ty.AnyStr]:
	"""Processes the given simplified matching spec, creating an equivalent :type:`Matcher` object"""
	if not recursive:
		return NoRecusionAdapterMatcher(
			matcher_from_spec(spec, recursive=True, period_special=period_special)
		)
	
	if spec is None:
		return DUMMY_MATCHER
	elif isinstance(spec, (str, bytes)):
		return GlobMatcher(spec, period_special=period_special)
	elif isinstance(spec, re_pattern_t):
		return ReMatcher(spec)
	elif isinstance(spec, collections.abc.Iterable) and not isinstance(spec, Matcher):
		return MetaMatcher(
			[matcher_from_spec(s, recursive=recursive, period_special=period_special) for s in spec]
		)
	else:
		return spec


class FSNodeType(enum.Enum):
	FILE = enum_auto()
	DIRECTORY = enum_auto()


#class FSNodeEntry(ty.NamedTuple):
#	type: FSNodeType
#	path: ty.AnyStr
#	relpath: ty.AnyStr
#	name: ty.AnyStr
#	parentfd: ty.Optional[int]

FSNodeEntry = ty.NamedTuple("FSNodeEntry", [
	("type", FSNodeType),
	("path", ty.AnyStr),
	("relpath", ty.AnyStr),
	("name", ty.AnyStr),
	("parentfd", ty.Optional[int])
])


class walk(ty.Iterator[FSNodeEntry], ty.Generic[ty.AnyStr]):
	__slots__ = ("_generator", "_close_fd")
	#_generator: ty.Generator[FSNodeEntry, ty.Any, None]
	#_close_fd: ty.Optional[int]
	
	def __init__(
			self,
			directory: ty.Union[utils.path_t, int],
			match_spec: match_spec_t[ty.AnyStr] = None, *,
			follow_symlinks: bool = False,
			intermediate_dirs: bool = True,
			period_special: bool = True,
			recursive: bool = True
	):
		"""
		Arguments
		---------
		directory
			Path to, or file descriptor of, directory to scan
		match_spec
			Matching rules for limiting the files and directories to include in
			the scan
			
			By default everything will be scanned and included.
		follow_symlinks
			Follow symbolic links while scanning?
		period_special
			The value to pass to the *period_special* argument of :class:`GlobMatcher`
			when constructing such an object from the given *match_spec*
		intermediate_dirs
			When reporting a file or directory first ensure that all containing
			directories were already reported
			
			Due to the way matching works, a matcher may only ask for its target
			files to be included but not care about the directories leading up
			to that file and this would cause the file to be reported without
			these intermediate directories to the caller as well. If you require
			these directories to be reported for consistency, this option will
			keep track of these intermediate paths and make it appear as if these
			had been included up-front.
		recursive
			Recurse into the given directory while scanning?
			
			If ``False`` this will wrap the given matcher inside
			:class:`NoRecusionAdapterMatcher` and hence prevent the scanner from
			doing any recursion.
		"""
		self._close_fd = None  # type: ty.Optional[int]
		
		# Convert directory path to string …
		directory_str = None  # type: ty.Optional[ty.AnyStr]
		if not isinstance(directory, int):
			if hasattr(os, "fspath"):  #PY36+
				directory_str = directory = os.fspath(directory)
			else:  #PY35
				directory_str = directory = utils.convert_path(directory)
			
			# … and possibly open it as a FD if this is supported by the platform
			#
			# Note: `os.fwalk` support for binary paths was only added in 3.7+.
			if HAVE_FWALK and (not isinstance(directory, bytes) or HAVE_FWALK_BYTES):
				self._close_fd = directory = os.open(directory, os.O_RDONLY | O_DIRECTORY)
		elif not HAVE_FWALK:
			raise NotImplementedError("Passing a file descriptor as directory is "
			                          "not supported on this platform")
		
		# Best-effort ensure that target directory exists if it is accessed by path
		if not isinstance(directory, int):
			os.stat(directory)
		
		# Create and store matcher object
		matcher = matcher_from_spec(  # type: Matcher[ty.AnyStr]
			match_spec, recursive=recursive, period_special=period_special
		)
		
		self._generator = self._walk(  # type: ty.Generator[FSNodeEntry, ty.Any, None]
			directory, directory_str, matcher, follow_symlinks, intermediate_dirs
		)
	
	def __iter__(self) -> 'walk[ty.AnyStr]':
		return self
	
	def __next__(self) -> FSNodeEntry:
		return next(self._generator)
	
	def __enter__(self) -> 'walk[ty.AnyStr]':
		return self
	
	def __exit__(self, *a) -> None:
		self.close()
	
	def close(self) -> None:
		try:
			self._generator.close()
		finally:
			if self._close_fd is not None:
				os.close(self._close_fd)
				self._close_fd = None
	
	@staticmethod
	def _join_dirs_and_files(dirnames: ty.List[ty.AnyStr], filenames: ty.List[ty.AnyStr]) \
	    -> ty.Iterator[ty.Tuple[ty.AnyStr, bool]]:
		for dirname in dirnames:
			yield (dirname, True)
		
		for filename in filenames:
			yield (filename, False)
	
	def _walk(
			self,
			directory: ty.Union[ty.AnyStr, int],
			directory_str: ty.Optional[ty.AnyStr],
			matcher: Matcher[ty.AnyStr],
			follow_symlinks: bool,
			intermediate_dirs: bool
	) -> ty.Generator[FSNodeEntry, ty.Any, None]:
		sep = (utils.maybe_fsencode(os.path.sep, directory_str)  # type: ty.AnyStr
		       if directory_str is not None else os.path.sep)
		dot = utils.maybe_fsencode(".", sep)  # type: ty.AnyStr
		
		# Identify the leading portion of the `dirpath` returned by `os.walk`
		# that should be dropped
		if not isinstance(directory, int):
			while directory.endswith(sep):
				directory = directory[:-len(sep)]
		prefix = (directory if not isinstance(directory, int) else dot) + sep
		
		reported_directories = set()  # type: ty.Set[ty.AnyStr]
		
		# Always report the top-level directory even if nothing therein is matched
		reported_directories.add(utils.maybe_fsencode("", sep))
		yield FSNodeEntry(
			type     = FSNodeType.DIRECTORY,
			path     = prefix[:-len(sep)],
			relpath  = dot,
			name     = dot,
			parentfd = None
		)
		
		if not isinstance(directory, int):
			walk_iter = os.walk(directory, followlinks=follow_symlinks)
		else:
			walk_iter = os.fwalk(dot, dir_fd=directory, follow_symlinks=follow_symlinks)
		try:
			for result in walk_iter:
				dirpath, dirnames, filenames = result[0:3]
				dirfd = result[3] if len(result) > 3 else None
				
				# Remove the directory prefix from the received path
				_, _, dirpath = dirpath.partition(prefix)
				
				# Keep track of reported intermediaries, so that we only check for
				# these at most once per directory base
				intermediates_reported = False  # type: bool
				
				for filename, is_dir in self._join_dirs_and_files(list(dirnames), filenames):
					filepath = os.path.join(dirpath, filename)
					
					# Check if matcher thinks we should descend into this directory
					if is_dir and not matcher.should_descend(filepath):
						dirnames.remove(filename)
					
					# Check if matcher thinks we should report this node
					if not matcher.should_report(filepath, is_dir=is_dir):
						continue
					
					# Ensure that all containing directories are reported
					# before reporting this node
					if not intermediates_reported and intermediate_dirs:
						parts = dirpath.split(sep)
						for end_offset in range(len(parts)):
							parent_dirpath = sep.join(parts[0:(end_offset + 1)])
							if parent_dirpath not in reported_directories:
								reported_directories.add(parent_dirpath)
								yield FSNodeEntry(
									type     = FSNodeType.DIRECTORY,
									path     = (prefix + parent_dirpath),
									relpath  = parent_dirpath,
									name     = parts[end_offset],
									parentfd = None
								)
						intermediates_reported = True
					
					# Report the target file or directory
					if is_dir:
						reported_directories.add(filepath)
						yield FSNodeEntry(
							type     = FSNodeType.DIRECTORY,
							path     = (prefix + filepath),
							relpath  = filepath,
							name     = filename,
							parentfd = dirfd
						)
					else:
						yield FSNodeEntry(
							type     = FSNodeType.FILE,
							path     = (prefix + filepath),
							relpath  = filepath,
							name     = filename,
							parentfd = dirfd
						)
		finally:
			# Make sure the file descriptors bound by `os.fwalk` are freed on error
			walk_iter.close()
			
			# Close root file descriptor of `os.fwalk` as well
			if self._close_fd is not None:
				os.close(self._close_fd)
				self._close_fd = None


if HAVE_FWALK:  # pragma: no cover
	supports_fd = frozenset({walk})  # type: ty.Set[ty.Callable[..., ty.Any]]
else:  # pragma: no cover
	supports_fd = frozenset()  # type: ty.Set[ty.Callable[..., ty.Any]]
