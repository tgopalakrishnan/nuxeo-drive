"""Main API to perform Nuxeo Drive operations"""

from time import time
from time import sleep
import os.path
import logging
from threading import local

from nxdrive.client import NuxeoClient
from nxdrive.client import LocalClient
from nxdrive.client import safe_filename
from nxdrive.model import get_scoped_session_maker
from nxdrive.model import ServerBinding
from nxdrive.model import RootBinding
from nxdrive.model import LastKnownState
from nxdrive.client import NotFound

from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy import asc
from sqlalchemy import not_
from sqlalchemy import or_


class Controller(object):
    """Manage configuration and perform Nuxeo Drive Operations

    This class is thread safe: instance can be shared by multiple threads
    as DB sessions and Nuxeo clients are thread locals.
    """

    def __init__(self, config_folder, nuxeo_client_factory=None, echo=None):
        if echo is None:
            echo = os.environ.get('NX_DRIVE_LOG_SQL', None) is not None
        self.config_folder = os.path.expanduser(config_folder)
        if not os.path.exists(self.config_folder):
            os.makedirs(self.config_folder)

        # Handle connection to the local Nuxeo Drive configuration and
        # metadata sqlite database.
        self._session_maker = get_scoped_session_maker(
            self.config_folder, echo=echo)

        # make it possible to pass an arbitrary nuxeo client factory
        # for testing
        if nuxeo_client_factory is not None:
            self.nuxeo_client_factory = nuxeo_client_factory
        else:
            self.nuxeo_client_factory = NuxeoClient

        self._local = local()

    def get_session(self):
        """Reuse the thread local session for this controller

        Using the controller in several thread should be thread safe as long as
        this method is always call
        """
        return self._session_maker()

    def start(self):
        """Start the Nuxeo Drive main daemon if not already started"""
        # TODO, see:
        # https://github.com/mozilla-services/circus/blob/master/circus/
        # circusd.py#L34

    def stop(self):
        """Stop the Nuxeo Drive daemon"""
        # TODO

    def children_states(self, folder_path, full_states=False):
        """List the status of the children of a folder

        The state of the folder is a summary of their descendant rather
        than their own instric synchronization step which is of little
        use for the end user.

        If full is True the full state object is returned instead of just the
        local path.
        """
        session = self.get_session()
        server_binding = self.get_server_binding(folder_path, session=session)
        if server_binding is not None:
            # TODO: if folder_path is the top level Nuxeo Drive folder, list
            # all the root binding states
            raise NotImplementedError(
                "Children States of a server binding is not yet implemented")

        # Find the root binding for this absolute path
        binding, path = self._binding_path(folder_path, session=session)

        try:
            folder_state = session.query(LastKnownState).filter_by(
                local_root=binding.local_root,
                path=path,
            ).one()
        except NoResultFound:
            return []

        states = self._pair_states_recursive(binding.local_root, session,
                                             folder_state)
        if full_states:
            return [(s, pair_state) for s, pair_state in states
                    if (s.parent_path == path
                        or s.remote_parent_ref == folder_state.remote_ref)]

        return [(s.path, pair_state) for s, pair_state in states
                if s.path is not None and s.parent_path == path]

    def _pair_states_recursive(self, local_root, session, doc_pair):
        """TODO: write me"""
        if not doc_pair.folderish:
            return [(doc_pair, doc_pair.pair_state)]

        if doc_pair.path is not None and doc_pair.remote_ref is not None:
            f = or_(
                LastKnownState.parent_path == doc_pair.path,
                LastKnownState.remote_parent_ref == doc_pair.remote_ref,
            )
        elif doc_pair.path is not None:
            f = LastKnownState.parent_path == doc_pair.path
        elif doc_pair.remote_ref is not None:
            f = LastKnownState.remote_parent_ref == doc_pair.remote_ref
        else:
            raise ValueError("Illegal state %r: at least path or remote_ref"
                             " should be not None." % doc_pair)

        children_states = session.query(LastKnownState).filter_by(
            local_root=local_root).filter(f).order_by(
                asc(LastKnownState.local_name),
                asc(LastKnownState.remote_name),
            ).all()

        results = []
        for child_state in children_states:
            sub_results = self._pair_states_recursive(
                local_root, session, child_state)
            results.extend(sub_results)

        # A folder stays synchronized (or unknown) only if all the descendants
        # are themselfves synchronized.
        pair_state = doc_pair.pair_state
        for _, sub_pair_state in results:
            if sub_pair_state != 'synchronized':
                pair_state = 'children_modified'
            break
        # Pre-pend the folder state to the descendants
        return [(doc_pair, pair_state)] + results

    def _binding_path(self, folder_path, session=None):
        """Find a root binding and relative path for a given FS path"""
        folder_path = os.path.abspath(folder_path)

        # Check exact root binding match
        binding = self.get_root_binding(folder_path, session=session)
        if binding is not None:
            return binding, '/'

        # Check for root bindings that are prefix of folder_path
        session = self.get_session()
        all_root_bindings = session.query(RootBinding).all()
        root_bindings = [rb for rb in all_root_bindings
                         if folder_path.startswith(
                             rb.local_root + os.path.sep)]
        if len(root_bindings) == 0:
            raise NotFound("Could not find any root binding for "
                               + folder_path)
        elif len(root_bindings) > 1:
            raise RuntimeError("Found more than one binding for %s: %r" % (
                folder_path, root_bindings))
        binding = root_bindings[0]
        path = folder_path[len(binding.local_root):]
        path.replace(os.path.sep, '/')
        return binding, path

    def get_server_binding(self, local_folder, raise_if_missing=False,
                           session=None):
        """Find the ServerBinding instance for a given local_folder"""
        if session is None:
            session = self.get_session()
        try:
            return session.query(ServerBinding).filter(
                ServerBinding.local_folder == local_folder).one()
        except NoResultFound:
            if raise_if_missing:
                raise RuntimeError(
                    "Folder '%s' is not bound to any Nuxeo server"
                    % local_folder)
            return None

    def bind_server(self, local_folder, server_url, username, password):
        """Bind a local folder to a remote nuxeo server"""
        session = self.get_session()
        try:
            server_binding = session.query(ServerBinding).filter(
                ServerBinding.local_folder == local_folder).one()
            raise RuntimeError(
                "%s is already bound to '%s' with user '%s'" % (
                    local_folder, server_binding.server_url,
                    server_binding.remote_user))
        except NoResultFound:
            # this is expected in most cases
            pass

        # check the connection to the server by issuing an authentication
        # request
        self.nuxeo_client_factory(server_url, username, password)

        session.add(ServerBinding(local_folder, server_url, username,
                                       password))
        session.commit()

    def unbind_server(self, local_folder):
        """Remove the binding to a Nuxeo server

        Local files are not deleted"""
        session = self.get_session()
        binding = self.get_server_binding(local_folder, raise_if_missing=True,
                                          session=session)
        session.delete(binding)
        session.commit()

    def get_root_binding(self, local_root, raise_if_missing=False,
                         session=None):
        """Find the RootBinding instance for a given local_root

        It is the responsability of the caller to commit any change in
        the same thread if needed.
        """
        if session is None:
            session = self.get_session()
        try:
            return session.query(RootBinding).filter(
                RootBinding.local_root == local_root).one()
        except NoResultFound:
            if raise_if_missing:
                raise RuntimeError(
                    "Folder '%s' is not bound as a root."
                    % local_root)
            return None

    def bind_root(self, local_folder, remote_root, repository='default'):
        """Bind local root to a remote root (folderish document in Nuxeo).

        local_folder must be already bound to an existing Nuxeo server. A
        new folder will be created under that folder to bind the remote
        root.

        remote_root must be the IdRef or PathRef of an existing folderish
        document on the remote server bound to the local folder. The
        user account must have write access to that folder, otherwise
        a RuntimeError will be raised.
        """
        # Check that local_root is a subfolder of bound folder
        session = self.get_session()
        server_binding = self.get_server_binding(local_folder,
                                                 raise_if_missing=True,
                                                 session=session)

        # Check the remote root exists and is an editable folder by current
        # user.
        nxclient = self.nuxeo_client_factory(server_binding.server_url,
                                             server_binding.remote_user,
                                             server_binding.remote_password,
                                             repository=repository,
                                             base_folder=remote_root)
        remote_info = nxclient.get_info(remote_root)
        if remote_info is None or not remote_info.folderish:
            raise RuntimeError(
                'No folder at "%s/%s" visible by "%s" on server "%s"'
                % (repository, remote_root, server_binding.remote_user,
                   server_binding.server_url))

        if not nxclient.check_writable(remote_root):
            raise RuntimeError(
                'Folder at "%s:%s" is not editable by "%s" on server "%s"'
                % (repository, remote_root, server_binding.remote_user,
                   server_binding.server_url))

        # Check that this workspace does not already exist locally
        # TODO: shall we handle deduplication for root names too?
        local_root = os.path.join(local_folder,
                                  safe_filename(remote_info.name))
        if os.path.exists(local_root):
            raise RuntimeError(
                'Cannot initialize binding to existing local folder: %s'
                % local_root)

        os.mkdir(local_root)
        lcclient = LocalClient(local_root)
        local_info = lcclient.get_info('/')

        # Register the binding itself
        session.add(RootBinding(local_root, repository, remote_root))

        # Initialize the metadata info by recursive walk on the remote folder
        # structure
        self._recursive_init(lcclient, local_info, nxclient, remote_info)
        session.commit()

    def _recursive_init(self, local_client, local_info, remote_client,
                        remote_info):
        """Initialize the metadata table by walking the binding tree"""

        folderish = remote_info.folderish
        state = LastKnownState(local_client.base_folder,
                               local_info=local_info,
                               remote_info=remote_info)
        if folderish:
            # Mark as synchronized as there is nothing to download later
            state.update_state(local_state='synchronized',
                               remote_state='synchronized')
        else:
            # Mark remote as updated to trigger a download of the binary
            # attachment during the next synchro
            state.update_state(local_state='synchronized',
                               remote_state='modified')
        session = self.get_session()
        session.add(state)

        if folderish:
            # TODO: how to handle the too many children case properly?
            # Shall we introduce some pagination or shall we raise an
            # exception if a folder contains too many children?
            children = remote_client.get_children_info(remote_info.uid)
            for child_remote_info in children:
                if child_remote_info.folderish:
                    child_local_path = local_client.make_folder(
                        local_info.path, child_remote_info.name)
                else:
                    child_local_path = local_client.make_file(
                        local_info.path, child_remote_info.name)
                child_local_info = local_client.get_info(child_local_path)
                self._recursive_init(local_client, child_local_info,
                                     remote_client, child_remote_info)

    def unbind_root(self, local_root, session=None):
        """Remove binding on a root folder"""
        if session is None:
            session = self.get_session()
        binding = self.get_root_binding(local_root, raise_if_missing=True,
                                        session=session)
        session.delete(binding)
        session.commit()

    def _scan_local(self, local_root, session):
        """Recursively scan the bound local folder looking for updates"""
        root_state = session.query(LastKnownState).filter_by(
            local_root=local_root, path='/').one()

        client = root_state.get_local_client()
        root_info = client.get_info('/')
        # recursive update
        self._scan_local_recursive(local_root, session, client,
                                   root_state, root_info)
        session.commit()

    def _mark_deleted_local_recursive(self, local_root, session, doc_pair):
        """Update the metadata of the descendants of locally deleted doc"""
        # delete descendants first
        children = session.query(LastKnownState).filter_by(
            local_root=local_root, parent_path=doc_pair.path).all()
        for child in children:
            self._mark_deleted_local_recursive(local_root, session, child)

        # update the state of the parent it-self
        if doc_pair.remote_ref is None:
            # Unbound child metadata can be removed
            session.delete(doc_pair)
        else:
            # mark it for remote deletion
            doc_pair.update_local(None)

    def _scan_local_recursive(self, local_root, session, client,
                              doc_pair, local_info):
        """Recursively scan the bound local folder looking for updates"""
        if local_info is None:
            raise ValueError("Cannot bind %r to missing local info" %
                             doc_pair)

        # Update the pair state from the collected local info
        doc_pair.update_local(local_info)

        if not local_info.folderish:
            # No children to align, early stop.
            return

        # detect recently deleted children
        children_info = client.get_children_info(local_info.path)
        children_path = set(c.path for c in children_info)

        q = session.query(LastKnownState).filter_by(
            local_root=local_root,
            parent_path=local_info.path,
        )
        if len(children_path) > 0:
            q = q.filter(not_(LastKnownState.path.in_(children_path)))

        for deleted in q.all():
            self._mark_deleted_local_recursive(local_root, session, deleted)

        # recursively update children
        for child_info in children_info:

            # TODO: detect whether this is a __digit suffix name and relax the
            # alignment queries accordingly
            child_name = os.path.basename(child_info.path)
            child_pair = session.query(LastKnownState).filter_by(
                local_root=local_root,
                path=child_info.path).first()

            if child_pair is None:
                # Try to find an existing remote doc that has not yet been
                # bound to any local file that would align with both name
                # and digest
                child_pair = session.query(LastKnownState).filter_by(
                    local_root=local_root,
                    path=None,
                    remote_parent_ref=doc_pair.remote_ref,
                    remote_name=child_name,
                    folderish=child_info.folderish,
                    remote_digest=child_info.get_digest(),
                ).first()

            if child_pair is None:
                # Previous attempt has failed: relax the digest constraint
                child_pair = session.query(LastKnownState).filter_by(
                    local_root=local_root,
                    path=None,
                    remote_parent_ref=doc_pair.remote_ref,
                    remote_name=child_name,
                    folderish=child_info.folderish,
                ).first()

            if child_pair is None:
                # Could not find any pair state to align to, create one
                child_pair = LastKnownState(local_root, local_info=child_info)
                session.add(child_pair)

            self._scan_local_recursive(local_root, session, client,
                                       child_pair, child_info)

    def _scan_remote(self, local_root, session):
        """Recursively scan the bound remote folder looking for updates"""
        root_state = session.query(LastKnownState).filter_by(
            local_root=local_root, path='/').one()

        client = self.get_remote_client(root_state)
        root_info = client.get_info(root_state.remote_ref)
        # recursive update
        self._scan_remote_recursive(local_root, session, client,
                                    root_state, root_info)
        session.commit()

    def _mark_deleted_remote_recursive(self, local_root, session, doc_pair):
        """Update the metadata of the descendants of remotely deleted doc"""
        # delete descendants first
        children = session.query(LastKnownState).filter_by(
            local_root=local_root,
            remote_parent_ref=doc_pair.remote_ref).all()
        for child in children:
            self._mark_deleted_remote_recursive(local_root, session, child)

        # update the state of the parent it-self
        if doc_pair.path is None:
            # Unbound child metadata can be removed
            session.delete(doc_pair)
        else:
            # schedule it for local deletion
            doc_pair.update_remote(None)

    def _scan_remote_recursive(self, local_root, session, client,
                               doc_pair, remote_info):
        """Recursively scan the bound remote folder looking for updates"""
        if remote_info is None:
            raise ValueError("Cannot bind %r to missing remote info" %
                             doc_pair)

        # Update the pair state from the collected remote info
        doc_pair.update_remote(remote_info)

        if not remote_info.folderish:
            # No children to align, early stop.
            return

        # detect recently deleted children
        children_info = client.get_children_info(remote_info.uid)
        children_refs = set(c.uid for c in children_info)

        q = session.query(LastKnownState).filter_by(
            local_root=local_root,
            remote_parent_ref=remote_info.uid,
        )
        if len(children_refs) > 0:
            q = q.filter(not_(LastKnownState.remote_ref.in_(children_refs)))

        for deleted in q.all():
            self._mark_deleted_remote_recursive(local_root, session, deleted)

        # recursively update children
        for child_info in children_info:

            # TODO: detect whether this is a __digit suffix name and relax the
            # alignment queries accordingly
            child_name = child_info.name
            child_pair = session.query(LastKnownState).filter_by(
                local_root=local_root,
                remote_ref=child_info.uid).first()

            if child_pair is None:
                # Try to find an existing local doc that has not yet been
                # bound to any remote file that would align with both name
                # and digest
                child_pair = session.query(LastKnownState).filter_by(
                    local_root=local_root,
                    remote_ref=None,
                    parent_path=doc_pair.path,
                    local_name=child_name,
                    folderish=child_info.folderish,
                    local_digest=child_info.get_digest(),
                ).first()

            if child_pair is None:
                # Previous attempt has failed: relax the digest constraint
                child_pair = session.query(LastKnownState).filter_by(
                    local_root=local_root,
                    remote_ref=None,
                    parent_path=doc_pair.path,
                    local_name=child_name,
                    folderish=child_info.folderish,
                ).first()

            if child_pair is None:
                # Could not find any pair state to align to, create one
                child_pair = LastKnownState(
                    local_root, remote_info=child_info)
                session.add(child_pair)

            self._scan_remote_recursive(local_root, session, client,
                                        child_pair, child_info)

    def refresh_remote_folders_from_log(root_binding):
        """Query the remote server audit log looking for state updates."""
        # TODO
        raise NotImplementedError()

    def list_pending(self, limit=100, local_root=None, session=None):
        """List pending files to synchronize, ordered by path

        Ordering by path makes it possible to synchronize sub folders content
        only once the parent folders have already been synchronized.
        """
        if session is None:
            session = self.get_session()
        if local_root is not None:
            return session.query(LastKnownState).filter(
                LastKnownState.pair_state != 'synchronized',
                LastKnownState.local_root == local_root
            ).order_by(
                asc(LastKnownState.path),
                asc(LastKnownState.remote_path),
            ).limit(limit).all()
        else:
            return session.query(LastKnownState).filter(
                LastKnownState.pair_state != 'synchronized'
            ).order_by(
                asc(LastKnownState.path),
                asc(LastKnownState.remote_path),
            ).limit(limit).all()

    def next_pending(self, local_root=None, session=None):
        """Return the next pending file to synchronize or None"""
        pending = self.list_pending(limit=1, local_root=local_root,
                                    session=session)
        return pending[0] if len(pending) > 0 else None

    def get_remote_client(self, doc_pair):
        """Fetch a client from the cache or create a new instance"""
        if not hasattr(self._local, 'remote_clients'):
            self._local.remote_clients = dict()
        cache = self._local.remote_clients
        remote_client = cache.get(doc_pair.local_root)
        if remote_client is None:
            remote_client = doc_pair.get_remote_client(
                self.nuxeo_client_factory)
            cache[doc_pair.local_root] = remote_client
        return remote_client

    def synchronize_one(self, doc_pair, session=None):
        """Refresh state a perform network transfer for a pair of documents."""
        if session is None:
            session = self.get_session()
        # Find a cached remote client for the server binding of the file to
        # synchronize
        remote_client = self.get_remote_client(doc_pair)
        # local clients are cheap
        local_client = doc_pair.get_local_client()

        # Update the status the collected info of this file to make sure
        # we won't perfom inconsistent operations

        if doc_pair.path is not None:
            doc_pair.refresh_local(local_client)
        if doc_pair.remote_ref is not None:
            remote_info = doc_pair.refresh_remote(remote_client)

        # Detect creation
        if (doc_pair.local_state == 'unknown'
            and doc_pair.remote_state == 'unknown'):
            if doc_pair.remote_ref is None and doc_pair.path is not None:
                doc_pair.update_state(local_state='created')
            if doc_pair.remote_ref is not None and doc_pair.path is None:
                doc_pair.update_state(remote_state='created')

        if len(session.dirty):
            # Make refreshed state immediately available to other
            # processes as file transfer can take a long time
            session.commit()

        # TODO: refactor blob access API to avoid loading content in memory
        # as python strings

        if doc_pair.pair_state == 'locally_modified':
            # TODO: handle smart versionning policy here (or maybe delegate to
            # a dedicated server-side operation)
            if doc_pair.remote_digest != doc_pair.local_digest:
                old_name = None
                if remote_info is not None:
                    old_name = remote_info.name
                remote_client.update_content(
                    doc_pair.remote_ref,
                    local_client.get_content(doc_pair.path),
                    name=old_name,
                )
                doc_pair.refresh_remote(remote_client)
            doc_pair.update_state('synchronized', 'synchronized')

        elif doc_pair.pair_state == 'remotely_modified':
            if doc_pair.remote_digest != doc_pair.local_digest:
                local_client.update_content(
                    doc_pair.path,
                    remote_client.get_content(doc_pair.remote_ref),
                )
                doc_pair.refresh_local(local_client)
            doc_pair.update_state('synchronized', 'synchronized')

        elif doc_pair.pair_state == 'locally_created':
            name = os.path.basename(doc_pair.path)
            # Find the parent pair to find the ref of the remote folder to
            # create the document
            parent_pair = session.query(LastKnownState).filter_by(
                local_root=doc_pair.local_root, path=doc_pair.parent_path
            ).one()
            parent_ref = parent_pair.remote_ref
            if parent_ref is None:
                logging.warn(
                    "Parent folder of %r/%r is not bound to a remote folder",
                    doc_pair.local_root, doc_pair.path)
                return
            if doc_pair.folderish:
                remote_ref = remote_client.make_folder(parent_ref, name)
            else:
                remote_ref = remote_client.make_file(
                    parent_ref, name,
                    content=local_client.get_content(doc_pair.path))
            doc_pair.update_remote(remote_client.get_info(remote_ref))
            doc_pair.update_state('synchronized', 'synchronized')

        elif doc_pair.pair_state == 'remotely_created':
            name = remote_info.name
            # Find the parent pair to find the path of the local folder to
            # create the document into
            parent_pair = session.query(LastKnownState).filter_by(
                local_root=doc_pair.local_root,
                remote_ref=remote_info.parent_uid,
            ).one()
            parent_path = parent_pair.path
            if parent_path is None:
                logging.warn(
                    "Parent folder of doc %r (%r:%r) is not bound to a local"
                    " folder",
                    name, doc_pair.remote_repo, doc_pair.remote_ref)
                return
            if doc_pair.folderish:
                path = local_client.make_folder(parent_path, name)
            else:
                path = local_client.make_file(
                    parent_path, name,
                    content=remote_client.get_content(doc_pair.remote_ref))
            doc_pair.update_local(local_client.get_info(path))
            doc_pair.update_state('synchronized', 'synchronized')

        elif doc_pair.pair_state == 'locally_deleted':
            if doc_pair.path == '/':
                # Special case: unbind root instead of performing deletion
                self.unbind_root(doc_pair.local_root, session=session)
            else:
                if doc_pair.remote_ref is not None:
                    # TODO: handle trash management with a dedicated server
                    # side operations?
                    remote_client.delete(doc_pair.remote_ref)
                # XXX: shall we also delete all the subcontent / folder at
                # once in the medata table?
                session.delete(doc_pair)

        elif doc_pair.pair_state == 'remotely_deleted':
            if doc_pair.path is not None:
                try:
                    # TODO: handle OS-specific trash management?
                    local_client.delete(doc_pair.path)
                    session.delete(doc_pair)
                    # XXX: shall we also delete all the subcontent / folder at
                    # once in the medata table?
                except OSError:
                    # Under Windows deletion can be impossible while another
                    # process is accessing the same file (e.g. word processor)
                    # TODO: be more specific as detecting this case
                    logging.debug("Deletion of %r/%r delayed due to concurrent"
                                  "editing of this file by another process.",
                                  doc_pair.local_root, doc_pair.path)
            else:
                session.delete(doc_pair)

        elif doc_pair.pair_state == 'deleted':
            # No need to store this information any further
            session.delete(doc_pair)
            session.commit()

        elif doc_pair.pair_state == 'conflicted':
            if doc_pair.local_digest == doc_pair.remote_digest != None:
                # Automated conflict resolution based on digest content:
                doc_pair.update_state('synchronized', 'synchronized')
        else:
            logging.warn("Unhandled pair_state: %r for %r" %
                         (doc_pair.pair_state, doc_pair))

        # TODO: handle other cases such as moves and lock updates

        # Ensure that concurrent process can monitor the synchronization
        # progress
        if len(session.dirty) != 0 or len(session.deleted) != 0:
            session.commit()

    def synchronize(self, limit=None, local_root=None, fault_tolerant=False):
        """Synchronize one file at a time from the pending list.

        Fault tolerant mode is meant to be skip problematic documents while not
        preventing the rest of the synchronization loop to work on documents
        that work as expected.

        This mode will probably hide real Nuxeo Drive bugs in the
        logs. It should thus not be enabled when running tests for the
        synchronization code but might be useful when running Nuxeo
        Drive in daemon mode.
        """
        synchronized = 0
        session = self.get_session()
        doc_pair = self.next_pending(local_root=local_root, session=session)
        while doc_pair is not None and (limit is None or synchronized < limit):
            if fault_tolerant:
                try:
                    self.synchronize_one(doc_pair, session=session)
                    synchronized += 1
                except Exception as e:
                    logging.error("Failed to synchronize %r: %r", doc_pair, e)
                    # TODO: flag pending and all descendant as failed with a time
                    # stamp and make next_pending ignore recently (e.g. up to 30s)
                    # failed synchronized pairs
                    raise NotImplementedError(
                        'Fault tolerant synchronization not implemented yet.')
            else:
                self.synchronize_one(doc_pair, session=session)
                synchronized += 1

            doc_pair = self.next_pending(local_root=local_root,
                                         session=session)
        return synchronized

    def loop(self, full_local_scan=True, full_remote_scan=True, delay=5,
             max_sync_step=50, max_loops=None, fault_tolerant=True):
        """Forever loop to scan / refresh states and perform synchronization

        delay is an delay in seconds that ensures that two consecutive
        scans won't happen too closely from one another.
        """
        # Instance flag to allow for another thread to interrupt the
        # synchronization loop cleanly
        self.continue_synchronization = True
        if not full_local_scan:
            # TODO: ensure that the watchdog thread for incremental state
            # update is started thread is started (and make sure it's able to
            # detect new bindings while running)
            raise NotImplementedError()

        previous_time = time()
        first_pass = True
        session = self.get_session()
        loop_count = 0
        while (self.continue_synchronization
               and (max_loops is None or loop_count <= max_loops)):
            for rb in session.query(RootBinding).all():
                has_done_scan = True
                try:
                    # the alternative to local full scan is the watchdog
                    # thread
                    if full_local_scan or first_pass:
                        self._scan_local(rb.local_root, session)
                        has_done_scan = True

                    if full_remote_scan or first_pass:
                        self._scan_remote(rb.local_root, session)
                        has_done_scan = True
                    else:
                        self.refresh_remote_from_log(rb.remote_ref)

                    self.synchronize(limit=max_sync_step,
                                     local_root=rb.local_root,
                                     fault_tolerant=fault_tolerant)
                except Exception as e:
                    # TODO: catch network related errors and log them at debug
                    # level instead as we expect the daemon to work even in
                    # offline mode without crashing
                    logging.error(e)

            # safety net to ensure that Nuxe Drive won't eat all the CPU, disk
            # and network resources of the machine scanning over an over the
            # bound folders too often.
            current_time = time()
            spent = current_time - previous_time
            if spent < delay and has_done_scan:
                sleep(delay - spent)
            previous_time = current_time
            first_pass = False
            loop_count += 1
