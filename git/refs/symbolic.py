import os
import re

from git.objects import (
						Object, 
						Commit
						)
from git.util import (
					join_path, 
					join_path_native, 
					to_native_path_linux,
					assure_directory_exists,
					join, 
					dirname,
					isdir,
					exists,
					isfile,
					rename,
					hex_to_bin,
					LockedFD
					)

from git.exc import BadObject
from log import RefLog

__all__ = ["SymbolicReference"]

class SymbolicReference(object):
	"""Represents a special case of a reference such that this reference is symbolic.
	It does not point to a specific commit, but to another Head, which itself 
	specifies a commit.
	
	A typical example for a symbolic reference is HEAD."""
	__slots__ = ("repo", "path")
	
	_resolve_ref_on_create = False
	_points_to_commits_only = True
	_common_path_default = ""
	_id_attribute_ = "name"
	
	re_hexsha_only = re.compile('^[0-9A-Fa-f]{40}$')
	
	#{ Configuration
	# Object class to be used when instantiating objects
	ObjectCls = Object
	CommitCls = Commit
	
	# all of the following are set by the package initializer
	HEADCls = None
	HeadCls = None
	RemoteReferenceCls = None
	TagReferenceCls = None
	ReferenceCls = None
	#}END configuration
	
	def __init__(self, repo, path):
		self.repo = repo
		self.path = path
		
	def __str__(self):
		return self.path
		
	def __repr__(self):
		return '<git.%s "%s">' % (self.__class__.__name__, self.path)
		
	def __eq__(self, other):
		return self.path == other.path
		
	def __ne__(self, other):
		return not ( self == other )
		
	def __hash__(self):
		return hash(self.path)
		
	@property
	def name(self):
		"""
		:return:
			In case of symbolic references, the shortest assumable name 
			is the path itself."""
		return self.path
	
	@property
	def abspath(self):
		return join_path_native(self.repo.git_dir, self.path)
		
	@classmethod
	def _get_packed_refs_path(cls, repo):
		return join(repo.git_dir, 'packed-refs')
		
	@classmethod
	def _iter_packed_refs(cls, repo):
		"""Returns an iterator yielding pairs of sha1/path pairs for the corresponding refs.
		:note: The packed refs file will be kept open as long as we iterate"""
		try:
			fp = open(cls._get_packed_refs_path(repo), 'rb')
			for line in fp:
				line = line.strip()
				if not line:
					continue
				if line.startswith('#'):
					if line.startswith('# pack-refs with:') and not line.endswith('peeled'):
						raise TypeError("PackingType of packed-Refs not understood: %r" % line)
					# END abort if we do not understand the packing scheme
					continue
				# END parse comment
				
				# skip dereferenced tag object entries - previous line was actual
				# tag reference for it
				if line[0] == '^':
					continue
				
				yield tuple(line.split(' ', 1))
			# END for each line
		except (OSError,IOError):
			raise StopIteration
		# END no packed-refs file handling 
		# NOTE: Had try-finally block around here to close the fp, 
		# but some python version woudn't allow yields within that.
		# I believe files are closing themselves on destruction, so it is 
		# alright.
		
	@classmethod
	def dereference_recursive(cls, repo, ref_path):
		"""
		:return: hexsha stored in the reference at the given ref_path, recursively dereferencing all
			intermediate references as required
		:param repo: the repository containing the reference at ref_path"""
		while True:
			hexsha, ref_path = cls._get_ref_info(repo, ref_path)
			if hexsha is not None:
				return hexsha
		# END recursive dereferencing
		
	@classmethod
	def _get_ref_info(cls, repo, ref_path):
		"""Return: (sha, target_ref_path) if available, the sha the file at 
		rela_path points to, or None. target_ref_path is the reference we 
		point to, or None"""
		tokens = None
		try:
			fp = open(join(repo.git_dir, ref_path), 'r')
			value = fp.read().rstrip()
			fp.close()
			tokens = value.split(" ")
		except (OSError,IOError):
			# Probably we are just packed, find our entry in the packed refs file
			# NOTE: We are not a symbolic ref if we are in a packed file, as these
			# are excluded explictly
			for sha, path in cls._iter_packed_refs(repo):
				if path != ref_path: continue
				tokens = (sha, path)
				break
			# END for each packed ref
		# END handle packed refs
		if tokens is None:
			raise ValueError("Reference at %r does not exist" % ref_path)
		
		# is it a reference ?
		if tokens[0] == 'ref:':
			return (None, tokens[1])
			
		# its a commit
		if cls.re_hexsha_only.match(tokens[0]):
			return (tokens[0], None)
			
		raise ValueError("Failed to parse reference information from %r" % ref_path)
	
	def _get_object_sha(self):
		"""
		:return:
			The binary sha to the object our ref currently refers to. Refs can be cached, they will 
			always point to the actual object as it gets re-created on each query"""
		return hex_to_bin(self.dereference_recursive(self.repo, self.path))
	
	def _get_object(self):
		"""
		:return:
			The object our ref currently refers to."""
		# have to be dynamic here as we may be a tag which can point to anything
		# Our path will be resolved to the hexsha which will be used accordingly
		return self.ObjectCls.new_from_sha(self.repo, self._get_object_sha())
	
	def set_object(self, object_id, logmsg = None):
		"""Set the object we point to, possibly dereference our symbolic reference first.
		If the reference does not exist, it will be created
		
		:param object: a reference specifier string, a SymbolicReference or an object hex sha. 
			SymbolicReferences will be dereferenced beforehand to obtain the object they point to
		:param logmsg: If not None, the message will be used in the reflog entry to be 
			written. Otherwise the reflog is not altered
		:note: plain SymbolicReferences may not actually point to objects by convention
		:return: self"""
		if isinstance(object_id, SymbolicReference):
			object = object.object
		#END resolve references
		
		is_detached = True
		try:
			is_detached = self.is_detached
		except ValueError:
			pass
		# END handle non-existing ones
		
		if is_detached:
			return self.set_reference(object_id, logmsg)
			
		# set the commit on our reference
		return self._get_reference().set_object(object_id, logmsg)
		
	def _get_commit(self):
		"""
		:return:
			Commit object we point to, works for detached and non-detached 
			SymbolicReferences. The symbolic reference will be dereferenced recursively."""
		obj = self._get_object()
		if obj.type == 'tag':
			obj = obj.object
		#END dereference tag
		
		if obj.type != self.CommitCls.type:
			raise TypeError("Symbolic Reference pointed to object %r, commit was required" % obj)
		#END handle type
		return obj
		
	def set_commit(self, commit, logmsg = None):
		"""As set_object, but restricts the type of object to be a Commit
		
		:raise ValueError: If commit is not a Commit object or doesn't point to 
			a commit
		:return: self"""
		# check the type - assume the best if it is a base-string
		is_invalid_type = False
		if isinstance(commit, self.ObjectCls):
			is_invalid_type = commit.type != self.CommitCls.type
		elif isinstance(commit, SymbolicReference):
			is_invalid_type = commit.object.type != self.CommitCls.type
		else:
			try:
				is_invalid_type = self.repo.resolve_object(commit).type != self.CommitCls.type
			except BadObject:
				raise ValueError("Invalid object: %s" % commit)
			#END handle exception
		# END verify type
		
		if is_invalid_type:
			raise ValueError("Need commit, got %r" % commit)
		#END handle raise
		
		# we leave strings to the rev-parse method below
		self.set_object(commit, logmsg)
		
		return self
		
	
	commit = property(_get_commit, set_commit, doc="Query or set commits directly")
	object = property(_get_object, set_object, doc="Return the object our ref currently refers to")
	object_binsha = property(_get_object_sha, set_object, doc="Return the object our ref currently refers to")
		
	def _get_reference(self):
		""":return: Reference Object we point to
		:raise TypeError: If this symbolic reference is detached, hence it doesn't point
			to a reference, but to a commit"""
		sha, target_ref_path = self._get_ref_info(self.repo, self.path)
		if target_ref_path is None:
			raise TypeError("%s is a detached symbolic reference as it points to %r" % (self, sha))
		return self.from_path(self.repo, target_ref_path)
		
	def set_reference(self, ref, logmsg = None):
		"""Set ourselves to the given ref. It will stay a symbol if the ref is a Reference.
		Otherwise an Object, given as Object instance or refspec, is assumed and if valid, 
		will be set which effectively detaches the refererence if it was a purely 
		symbolic one.
		
		:param ref: SymbolicReference instance, hexadecimal sha string or refspec string
			Only if the ref is a SymbolicRef instance, we will point to it. Everthiny
			else is dereferenced to obtain the actual object.
		:param logmsg: If set to a string, the message will be used in the reflog.
			Otherwise, a reflog entry is not written for the changed reference.
			The previous commit of the entry will be the commit we point to now.
			
			See also: log_append()
		
		:return: self
		:note: This symbolic reference will not be dereferenced. For that, see 
			``set_object(...)``"""
		write_value = None
		obj = None
		if isinstance(ref, SymbolicReference):
			write_value = "ref: %s" % ref.path
		elif isinstance(ref, self.ObjectCls):
			obj = ref
			write_value = ref.hexsha
		elif isinstance(ref, basestring):
			try:
				obj = self.repo.resolve_object(ref+"^{}")	# optionally deref tags
				write_value = obj.hexsha
			except BadObject:
				raise ValueError("Could not extract object from %s" % ref)
			# END end try string
		else:
			raise ValueError("Unrecognized Value: %r" % ref)
		# END try commit attribute
		
		# typecheck
		if obj is not None and self._points_to_commits_only and obj.type != Commit.type:
			raise TypeError("Require commit, got %r" % obj)
		#END verify type
		
		oldbinsha = None
		if logmsg is not None:
			try:
				oldbinsha = self.commit.binsha
			except ValueError:
				oldbinsha = Commit.NULL_BIN_SHA
			#END handle non-existing
		#END retrieve old hexsha
		
		fpath = self.abspath
		assure_directory_exists(fpath, is_file=True)
		
		lfd = LockedFD(fpath)
		fd = lfd.open(write=True, stream=True)
		fd.write(write_value)
		lfd.commit()
		
		# Adjust the reflog
		if logmsg is not None:
			self.log_append(oldbinsha, logmsg)
		#END handle reflog
		
		return self
		

	# aliased reference
	reference = property(_get_reference, set_reference, doc="Returns the Reference we point to")
	ref = reference
	
	def is_valid(self):
		"""
		:return:
			True if the reference is valid, hence it can be read and points to 
			a valid object or reference."""
		try:
			self.object
		except (OSError, ValueError, BadObject):
			return False
		else:
			return True
		
	@property
	def is_detached(self):
		"""
		:return:
			True if we are a detached reference, hence we point to a specific commit
			instead to another reference"""
		try:
			self.ref
			return False
		except TypeError:
			return True
	
	def log(self):
		"""
		:return: RefLog for this reference. Its last entry reflects the latest change
			applied to this reference
			
		.. note:: As the log is parsed every time, its recommended to cache it for use
			instead of calling this method repeatedly. It should be considered read-only."""
		return RefLog.from_file(RefLog.path(self))
		
	def log_append(self, oldbinsha, message, newbinsha=None):
		"""Append a logentry to the logfile of this ref
		
		:param oldbinsha: binary sha this ref used to point to
		:param message: A message describing the change
		:param newbinsha: The sha the ref points to now. If None, our current commit sha
			will be used
		:return: added RefLogEntry instance"""
		return RefLog.append_entry(self.repo.config_reader(), RefLog.path(self), oldbinsha, 
									(newbinsha is None and self.commit.binsha) or newbinsha, 
									message) 

	def log_entry(self, index):
		""":return: RefLogEntry at the given index
		:param index: python list compatible positive or negative index
		
		.. note:: This method must read part of the reflog during execution, hence 
			it should be used sparringly, or only if you need just one index.
			In that case, it will be faster than the ``log()`` method"""
		return RefLog.entry_at(RefLog.path(self), index)

	@classmethod
	def to_full_path(cls, path):
		"""
		:return: string with a full repository-relative path which can be used to initialize 
			a Reference instance, for instance by using ``Reference.from_path``"""
		if isinstance(path, SymbolicReference):
			path = path.path
		full_ref_path = path
		if not cls._common_path_default:
			return full_ref_path
		if not path.startswith(cls._common_path_default+"/"):
			full_ref_path = '%s/%s' % (cls._common_path_default, path)
		return full_ref_path
	
	@classmethod
	def delete(cls, repo, path):
		"""Delete the reference at the given path
		
		:param repo:
			Repository to delete the reference from
		
		:param path:
			Short or full path pointing to the reference, i.e. refs/myreference
			or just "myreference", hence 'refs/' is implied.
			Alternatively the symbolic reference to be deleted"""
		full_ref_path = cls.to_full_path(path)
		abs_path = join(repo.git_dir, full_ref_path)
		if exists(abs_path):
			os.remove(abs_path)
		else:
			# check packed refs
			pack_file_path = cls._get_packed_refs_path(repo)
			try:
				reader = open(pack_file_path, 'rb')
			except (OSError,IOError):
				pass # it didnt exist at all
			else:
				new_lines = list()
				made_change = False
				dropped_last_line = False
				for line in reader:
					# keep line if it is a comment or if the ref to delete is not 
					# in the line
					# If we deleted the last line and this one is a tag-reference object, 
					# we drop it as well
					if ( line.startswith('#') or full_ref_path not in line ) and \
						( not dropped_last_line or dropped_last_line and not line.startswith('^') ):
						new_lines.append(line)
						dropped_last_line = False
						continue
					# END skip comments and lines without our path
					
					# drop this line
					made_change = True
					dropped_last_line = True
				# END for each line in packed refs
				reader.close()
				
				# write the new lines
				if made_change:
					# write-binary is required, otherwise windows will
					# open the file in text mode and change LF to CRLF !
					open(pack_file_path, 'wb').writelines(new_lines)
				# END write out file
			# END open exception handling
		# END handle deletion
		
		# delete the reflog
		reflog_path = RefLog.path(cls(repo, full_ref_path))
		if os.path.isfile(reflog_path):
			os.remove(reflog_path)
		#END remove reflog
		
			
	@classmethod
	def _create(cls, repo, path, resolve, reference, force, logmsg=None):
		"""internal method used to create a new symbolic reference.
		If resolve is False, the reference will be taken as is, creating 
		a proper symbolic reference. Otherwise it will be resolved to the 
		corresponding object and a detached symbolic reference will be created
		instead"""
		full_ref_path = cls.to_full_path(path)
		abs_ref_path = join(repo.git_dir, full_ref_path)
		
		# figure out target data
		target = reference
		if resolve:
			# could just use the resolve method, but it could be expensive
			# so we handle most common cases ourselves
			if isinstance(reference, cls.ObjectCls):
				target = reference.hexsha
			elif isinstance(reference, SymbolicReference):
				target = reference.object.hexsha
			else:
				target = repo.resolve_object(str(reference))
			#END handle resoltion
		#END need resolution
			
		if not force and isfile(abs_ref_path):
			target_data = str(target)
			if isinstance(target, SymbolicReference):
				target_data = target.path
			if not resolve:
				target_data = "ref: " + target_data
			existing_data = open(abs_ref_path, 'rb').read().strip() 
			if existing_data != target_data:
				raise OSError("Reference at %r does already exist, pointing to %r, requested was %r" % (full_ref_path, existing_data, target_data))
		# END no force handling
		
		ref = cls(repo, full_ref_path)
		ref.set_reference(target, logmsg)
		return ref
		
	@classmethod
	def create(cls, repo, path, reference='HEAD', force=False, logmsg=None):
		"""Create a new symbolic reference, hence a reference pointing to another reference.
		
		:param repo:
			Repository to create the reference in 
			
		:param path:
			full path at which the new symbolic reference is supposed to be 
			created at, i.e. "NEW_HEAD" or "symrefs/my_new_symref"
			
		:param reference:
			The reference to which the new symbolic reference should point to.
			If it is a commit'ish, the symbolic ref will be detached.
		
		:param force:
			if True, force creation even if a symbolic reference with that name already exists.
			Raise OSError otherwise
			
		:param logmsg:
			If not None, the message to append to the reflog. Otherwise no reflog
			entry is written.
			
		:return: Newly created symbolic Reference
			
		:raise OSError:
			If a (Symbolic)Reference with the same name but different contents
			already exists.
		
		:note: This does not alter the current HEAD, index or Working Tree"""
		return cls._create(repo, path, cls._resolve_ref_on_create, reference, force, logmsg)
	
	def rename(self, new_path, force=False):
		"""Rename self to a new path
		
		:param new_path:
			Either a simple name or a full path, i.e. new_name or features/new_name.
			The prefix refs/ is implied for references and will be set as needed.
			In case this is a symbolic ref, there is no implied prefix
			
		:param force:
			If True, the rename will succeed even if a head with the target name
			already exists. It will be overwritten in that case
			
		:return: self
		:raise OSError: In case a file at path but a different contents already exists """
		new_path = self.to_full_path(new_path)
		if self.path == new_path:
			return self
		
		new_abs_path = join(self.repo.git_dir, new_path)
		cur_abs_path = join(self.repo.git_dir, self.path)
		if isfile(new_abs_path):
			if not force:
				# if they point to the same file, its not an error
				if open(new_abs_path,'rb').read().strip() != open(cur_abs_path,'rb').read().strip():
					raise OSError("File at path %r already exists" % new_abs_path)
				# else: we could remove ourselves and use the otherone, but 
				# but clarity we just continue as usual
			# END not force handling
			os.remove(new_abs_path)
		# END handle existing target file
		
		dname = dirname(new_abs_path)
		if not isdir(dname):
			os.makedirs(dname)
		# END create directory
		
		rename(cur_abs_path, new_abs_path)
		self.path = new_path
		
		return self
		
	@classmethod
	def _iter_items(cls, repo, common_path = None):
		if common_path is None:
			common_path = cls._common_path_default
		rela_paths = set()
		
		# walk loose refs
		# Currently we do not follow links 
		for root, dirs, files in os.walk(join_path_native(repo.git_dir, common_path)):
			if 'refs/' not in root: # skip non-refs subfolders
				refs_id = [ d for d in dirs if d == 'refs' ]
				if refs_id:
					dirs[0:] = ['refs']
			# END prune non-refs folders
			
			for f in files:
				abs_path = to_native_path_linux(join_path(root, f))
				rela_paths.add(abs_path.replace(to_native_path_linux(repo.git_dir) + '/', ""))
			# END for each file in root directory
		# END for each directory to walk
		
		# read packed refs
		for sha, rela_path in cls._iter_packed_refs(repo):
			if rela_path.startswith(common_path):
				rela_paths.add(rela_path)
			# END relative path matches common path
		# END packed refs reading
		
		# return paths in sorted order
		for path in sorted(rela_paths):
			try:
				yield cls.from_path(repo, path)
			except ValueError:
				continue
		# END for each sorted relative refpath
		
	@classmethod
	def iter_items(cls, repo, common_path = None):
		"""Find all refs in the repository

		:param repo: is the repo

		:param common_path:
			Optional keyword argument to the path which is to be shared by all
			returned Ref objects.
			Defaults to class specific portion if None assuring that only 
			refs suitable for the actual class are returned.

		:return:
			git.SymbolicReference[], each of them is guaranteed to be a *only* a symbolic
			ref, or a derived class which is not detached
			
			List is lexigraphically sorted
			The returned objects represent actual subclasses, such as Head or TagReference"""
		return ( r for r in cls._iter_items(repo, common_path) if r.__class__ == cls or not r.is_detached )
		
	@classmethod
	def from_path(cls, repo, path):
		"""
		:param path: full .git-directory-relative path name to the Reference to instantiate
		:note: use to_full_path() if you only have a partial path of a known Reference Type
		:return:
			Instance of type Reference, Head, or Tag
			depending on the given path"""
		if not path:
			raise ValueError("Cannot create Reference from %r" % path)
		
		for ref_type in (cls.HEADCls, cls.HeadCls, cls.RemoteReferenceCls, cls.TagReferenceCls, cls.ReferenceCls, cls):
			try:
				instance = ref_type(repo, path)
				if instance.__class__ == SymbolicReference and instance.is_detached:
					raise ValueError("SymbolRef was detached, we drop it")
				return instance
			except ValueError:
				pass
			# END exception handling
		# END for each type to try
		raise ValueError("Could not find reference type suitable to handle path %r" % path)
