##############################################################################
#
# Copyright (c) 2001, 2002 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE
#
##############################################################################
"""Test suite for ZEO based on ZODB.tests."""
from __future__ import print_function
import multiprocessing
import re

from ..ClientStorage import ClientStorage
from .forker import get_port
from . import forker, Cache, CommitLockTests, ThreadTests
from . import IterationTests
from ..zrpc.error import DisconnectedError
from .._compat import PY3
from ZODB.tests import StorageTestBase, BasicStorage,  \
     TransactionalUndoStorage,  \
     PackableStorage, Synchronization, ConflictResolution, RevisionStorage, \
     MTStorage, ReadOnlyStorage, IteratorStorage, RecoveryStorage
from ZODB.tests.MinPO import MinPO
from ZODB.tests.StorageTestBase import zodb_unpickle
from ZODB.utils import p64, u64
from zope.testing import renormalizing

import doctest
import logging
import os
import persistent
import pprint
import re
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
import transaction
import unittest
from .. import ServerStub
from .. import StorageServer
from . import ConnectionTests
from ..zrpc import client as zrpc_client, connection as zrpc_connection
import ZODB
import ZODB.blob
import ZODB.tests.hexstorage
import ZODB.tests.testblob
import ZODB.tests.util
import ZODB.utils
import zope.testing.setupstack

logger = logging.getLogger('__name__')

from .. import connection, DB, __name__ as ZEO__name__

class DummyDB:
    def invalidate(self, *args):
        pass
    def invalidateCache(*unused):
        pass
    transform_record_data = untransform_record_data = lambda self, v: v


class CreativeGetState(persistent.Persistent):
    def __getstate__(self):
        self.name = 'me'
        return super(CreativeGetState, self).__getstate__()


class MiscZEOTests:
    """ZEO tests that don't fit in elsewhere."""

    def checkCreativeGetState(self):
        # This test covers persistent objects that provide their own
        # __getstate__ which modifies the state of the object.
        # For details see bug #98275

        db = ZODB.DB(self._storage)
        cn = db.open()
        rt = cn.root()
        m = CreativeGetState()
        m.attr = 'hi'
        rt['a'] = m

        # This commit used to fail because of the `Mine` object being put back
        # into `changed` state although it was already stored causing the ZEO
        # cache to bail out.
        transaction.commit()
        cn.close()

    def checkLargeUpdate(self):
        obj = MinPO("X" * (10 * 128 * 1024))
        self._dostore(data=obj)

    def checkZEOInvalidation(self):
        addr = self._storage._addr
        storage2 = self._wrap_client(
            ClientStorage(addr, wait=1, min_disconnect_poll=0.1))
        try:
            oid = self._storage.new_oid()
            ob = MinPO('first')
            revid1 = self._dostore(oid, data=ob)
            data, serial = storage2.load(oid, '')
            self.assertEqual(zodb_unpickle(data), MinPO('first'))
            self.assertEqual(serial, revid1)
            revid2 = self._dostore(oid, data=MinPO('second'), revid=revid1)

            # Now, storage 2 should eventually get the new data. It
            # will take some time, although hopefully not much.
            # We'll poll till we get it and whine if we time out:
            for n in range(30):
                time.sleep(.1)
                data, serial = storage2.load(oid, '')
                if (serial == revid2 and
                    zodb_unpickle(data) == MinPO('second')
                    ):
                    break
            else:
                raise AssertionError('Invalidation message was not sent!')
        finally:
            storage2.close()

    def checkVolatileCacheWithImmediateLastTransaction(self):
        # Earlier, a ClientStorage would not have the last transaction id
        # available right after successful connection, this is required now.
        addr = self._storage._addr
        storage2 = ClientStorage(addr)
        self.assert_(storage2.is_connected())
        self.assertEquals(ZODB.utils.z64, storage2.lastTransaction())
        storage2.close()

        self._dostore()
        storage3 = ClientStorage(addr)
        self.assert_(storage3.is_connected())
        self.assertEquals(8, len(storage3.lastTransaction()))
        self.assertNotEquals(ZODB.utils.z64, storage3.lastTransaction())
        storage3.close()

class ConfigurationTests(unittest.TestCase):

    def checkDropCacheRatherVerifyConfiguration(self):
        from ZODB.config import storageFromString
        # the default is to do verification and not drop the cache
        cs = storageFromString('''
        <zeoclient>
          server localhost:9090
          wait false
        </zeoclient>
        ''')
        self.assertEqual(cs._drop_cache_rather_verify, False)
        cs.close()
        # now for dropping
        cs = storageFromString('''
        <zeoclient>
          server localhost:9090
          wait false
          drop-cache-rather-verify true
        </zeoclient>
        ''')
        self.assertEqual(cs._drop_cache_rather_verify, True)
        cs.close()


class GenericTests(
    # Base class for all ZODB tests
    StorageTestBase.StorageTestBase,
    # ZODB test mixin classes (in the same order as imported)
    BasicStorage.BasicStorage,
    PackableStorage.PackableStorage,
    Synchronization.SynchronizedStorage,
    MTStorage.MTStorage,
    ReadOnlyStorage.ReadOnlyStorage,
    # ZEO test mixin classes (in the same order as imported)
    CommitLockTests.CommitLockVoteTests,
    ThreadTests.ThreadTests,
    # Locally defined (see above)
    MiscZEOTests,
    ):

    """Combine tests from various origins in one class."""

    shared_blob_dir = False
    blob_cache_dir = None

    def setUp(self):
        StorageTestBase.StorageTestBase.setUp(self)
        logger.info("setUp() %s", self.id())
        port = get_port(self)
        zconf = forker.ZEOConfig(('', port))
        zport, adminaddr, pid, path = forker.start_zeo_server(self.getConfig(),
                                                              zconf, port)
        self._pids = [pid]
        self._servers = [adminaddr]
        self._conf_path = path
        if not self.blob_cache_dir:
            # This is the blob cache for ClientStorage
            self.blob_cache_dir = tempfile.mkdtemp(
                'blob_cache',
                dir=os.path.abspath(os.getcwd()))
        self._storage = self._wrap_client(ClientStorage(
            zport, '1', cache_size=20000000,
            min_disconnect_poll=0.5, wait=1,
            wait_timeout=60, blob_dir=self.blob_cache_dir,
            shared_blob_dir=self.shared_blob_dir))
        self._storage.registerDB(DummyDB())

    def _wrap_client(self, client):
        return client

    def tearDown(self):
        self._storage.close()
        for server in self._servers:
            forker.shutdown_zeo_server(server)
        if hasattr(os, 'waitpid'):
            # Not in Windows Python until 2.3
            for pid in self._pids:
                os.waitpid(pid, 0)
        StorageTestBase.StorageTestBase.tearDown(self)

    def runTest(self):
        try:
            super(GenericTests, self).runTest()
        except:
            self._failed = True
            raise
        else:
            self._failed = False

    def open(self, read_only=0):
        # Needed to support ReadOnlyStorage tests.  Ought to be a
        # cleaner way.
        addr = self._storage._addr
        self._storage.close()
        self._storage = ClientStorage(addr, read_only=read_only, wait=1)

    def checkWriteMethods(self):
        # ReadOnlyStorage defines checkWriteMethods.  The decision
        # about where to raise the read-only error was changed after
        # Zope 2.5 was released.  So this test needs to detect Zope
        # of the 2.5 vintage and skip the test.

        # The __version__ attribute was not present in Zope 2.5.
        if hasattr(ZODB, "__version__"):
            ReadOnlyStorage.ReadOnlyStorage.checkWriteMethods(self)

    def checkSortKey(self):
        key = '%s:%s' % (self._storage._storage, self._storage._server_addr)
        self.assertEqual(self._storage.sortKey(), key)

    def _do_store_in_separate_thread(self, oid, revid, voted):

        def do_store():
            store = ClientStorage(self._storage._addr)
            try:
                t = transaction.get()
                store.tpc_begin(t)
                store.store(oid, revid, b'x', '', t)
                store.tpc_vote(t)
                store.tpc_finish(t)
            except Exception as v:
                import traceback
                print('E'*70)
                print(v)
                traceback.print_exception(*sys.exc_info())
            finally:
                store.close()

        thread = threading.Thread(name='T2', target=do_store)
        thread.setDaemon(True)
        thread.start()
        thread.join(voted and .1 or 9)
        return thread

class FullGenericTests(
    GenericTests,
    Cache.TransUndoStorageWithCache,
    ConflictResolution.ConflictResolvingStorage,
    ConflictResolution.ConflictResolvingTransUndoStorage,
    PackableStorage.PackableUndoStorage,
    RevisionStorage.RevisionStorage,
    TransactionalUndoStorage.TransactionalUndoStorage,
    IteratorStorage.IteratorStorage,
    IterationTests.IterationTests,
    ):
    """Extend GenericTests with tests that MappingStorage can't pass."""

class FileStorageRecoveryTests(StorageTestBase.StorageTestBase,
                               RecoveryStorage.RecoveryStorage):

    def getConfig(self):
        return """\
        <filestorage 1>
        path %s
        </filestorage>
        """ % tempfile.mktemp(dir='.')

    def _new_storage(self):
        port = get_port(self)
        zconf = forker.ZEOConfig(('', port))
        zport, adminaddr, pid, path = forker.start_zeo_server(self.getConfig(),
                                                              zconf, port)
        self._pids.append(pid)
        self._servers.append(adminaddr)

        blob_cache_dir = tempfile.mkdtemp(dir='.')

        storage = ClientStorage(
            zport, '1', cache_size=20000000,
            min_disconnect_poll=0.5, wait=1,
            wait_timeout=60, blob_dir=blob_cache_dir)
        storage.registerDB(DummyDB())
        return storage

    def setUp(self):
        StorageTestBase.StorageTestBase.setUp(self)
        self._pids = []
        self._servers = []

        self._storage = self._new_storage()
        self._dst = self._new_storage()

    def tearDown(self):
        self._storage.close()
        self._dst.close()

        for server in self._servers:
            forker.shutdown_zeo_server(server)
        if hasattr(os, 'waitpid'):
            # Not in Windows Python until 2.3
            for pid in self._pids:
                os.waitpid(pid, 0)
        StorageTestBase.StorageTestBase.tearDown(self)

    def new_dest(self):
        return self._new_storage()


class FileStorageTests(FullGenericTests):
    """Test ZEO backed by a FileStorage."""

    def getConfig(self):
        return """\
        <filestorage 1>
        path Data.fs
        </filestorage>
        """

    _expected_interfaces = (
        ('ZODB.interfaces', 'IStorageRestoreable'),
        ('ZODB.interfaces', 'IStorageIteration'),
        ('ZODB.interfaces', 'IStorageUndoable'),
        ('ZODB.interfaces', 'IStorageCurrentRecordIteration'),
        ('ZODB.interfaces', 'IExternalGC'),
        ('ZODB.interfaces', 'IStorage'),
        ('zope.interface', 'Interface'),
        )

    def checkInterfaceFromRemoteStorage(self):
        # ClientStorage itself doesn't implement IStorageIteration, but the
        # FileStorage on the other end does, and thus the ClientStorage
        # instance that is connected to it reflects this.
        self.failIf(ZODB.interfaces.IStorageIteration.implementedBy(
            ClientStorage))
        self.failUnless(ZODB.interfaces.IStorageIteration.providedBy(
            self._storage))
        # This is communicated using ClientStorage's _info object:
        self.assertEquals(self._expected_interfaces,
            self._storage._info['interfaces']
            )

class FileStorageHexTests(FileStorageTests):
    _expected_interfaces = (
        ('ZODB.interfaces', 'IStorageRestoreable'),
        ('ZODB.interfaces', 'IStorageIteration'),
        ('ZODB.interfaces', 'IStorageUndoable'),
        ('ZODB.interfaces', 'IStorageCurrentRecordIteration'),
        ('ZODB.interfaces', 'IExternalGC'),
        ('ZODB.interfaces', 'IStorage'),
        ('ZODB.interfaces', 'IStorageWrapper'),
        ('zope.interface', 'Interface'),
        )

    def getConfig(self):
        return """\
        %import ZODB.tests
        <hexstorage>
        <filestorage 1>
        path Data.fs
        </filestorage>
        </hexstorage>
        """

class FileStorageClientHexTests(FileStorageHexTests):

    def getConfig(self):
        return """\
        %import ZODB.tests
        <serverhexstorage>
        <filestorage 1>
        path Data.fs
        </filestorage>
        </serverhexstorage>
        """

    def _wrap_client(self, client):
        return ZODB.tests.hexstorage.HexStorage(client)



class MappingStorageTests(GenericTests):
    """ZEO backed by a Mapping storage."""

    def getConfig(self):
        return """<mappingstorage 1/>"""

    def checkSimpleIteration(self):
        # The test base class IteratorStorage assumes that we keep undo data
        # to construct our iterator, which we don't, so we disable this test.
        pass

    def checkUndoZombie(self):
        # The test base class IteratorStorage assumes that we keep undo data
        # to construct our iterator, which we don't, so we disable this test.
        pass

class DemoStorageTests(
    GenericTests,
    ):

    def getConfig(self):
        return """
        <demostorage 1>
          <filestorage 1>
             path Data.fs
          </filestorage>
        </demostorage>
        """

    def checkUndoZombie(self):
        # The test base class IteratorStorage assumes that we keep undo data
        # to construct our iterator, which we don't, so we disable this test.
        pass

    def checkPackWithMultiDatabaseReferences(self):
        pass # DemoStorage pack doesn't do gc
    checkPackAllRevisions = checkPackWithMultiDatabaseReferences

class HeartbeatTests(ConnectionTests.CommonSetupTearDown):
    """Make sure a heartbeat is being sent and that it does no harm

    This is really hard to test properly because we can't see the data
    flow between the client and server and we can't really tell what's
    going on in the server very well. :(

    """

    def setUp(self):
        # Crank down the select frequency
        self.__old_client_timeout = zrpc_client.client_timeout
        zrpc_client.client_timeout = self.__client_timeout
        ConnectionTests.CommonSetupTearDown.setUp(self)

    __client_timeouts = 0
    def __client_timeout(self):
        self.__client_timeouts += 1
        return .1

    def tearDown(self):
        zrpc_client.client_timeout = self.__old_client_timeout
        ConnectionTests.CommonSetupTearDown.tearDown(self)

    def getConfig(self, path, create, read_only):
        return """<mappingstorage 1/>"""

    def checkHeartbeatWithServerClose(self):
        # This is a minimal test that mainly tests that the heartbeat
        # function does no harm.
        self._storage = self.openClientStorage()
        client_timeouts = self.__client_timeouts
        forker.wait_until('got a timeout',
                          lambda : self.__client_timeouts > client_timeouts
                          )
        self._dostore()

        if hasattr(os, 'kill') and hasattr(signal, 'SIGKILL'):
            # Kill server violently, in hopes of provoking problem
            os.kill(self._pids[0], signal.SIGKILL)
            self._servers[0] = None
        else:
            self.shutdownServer()

        forker.wait_until('disconnected',
                          lambda : not self._storage.is_connected()
                          )
        self._storage.close()


class ZRPCConnectionTests(ConnectionTests.CommonSetupTearDown):

    def getConfig(self, path, create, read_only):
        return """<mappingstorage 1/>"""

    def checkCatastrophicClientLoopFailure(self):
        # Test what happens when the client loop falls over
        self._storage = self.openClientStorage()

        class Evil:
            def writable(self):
                raise SystemError("I'm evil")

        import zope.testing.loggingsupport
        handler = zope.testing.loggingsupport.InstalledHandler(
            zrpc_client.__name__)

        self._storage._rpc_mgr.map[None] = Evil()

        try:
            self._storage._rpc_mgr.trigger.pull_trigger()
        except DisconnectedError:
            pass

        forker.wait_until(
            'disconnected',
            lambda : not self._storage.is_connected()
            )

        log = str(handler)
        handler.uninstall()
        self.assert_("ZEO client loop failed" in log)
        self.assert_("Couldn't close a dispatcher." in log)

    def checkExceptionLogsAtError(self):
        # Test the exceptions are logged at error
        self._storage = self.openClientStorage()
        conn = self._storage._connection
        # capture logging
        log = []
        conn.logger.log = (
            lambda l, m, *a, **kw: log.append((l,m % a, kw))
            )

        # This is a deliberately bogus call to get an exception
        # logged
        self._storage._connection.handle_request(
            'foo', 0, 'history', (1, 2, 3, 4))
        # test logging

        py2_msg = (
            'history() raised exception: history() takes at most '
            '3 arguments (5 given)'
            )
        py32_msg = (
            'history() raised exception: history() takes at most '
            '3 positional arguments (5 given)'
            )
        py3_msg = (
            'history() raised exception: history() takes '
            'from 2 to 3 positional arguments but 5 were given'
            )
        for level, message, kw in log:
            if (message.endswith(py2_msg) or
                message.endswith(py32_msg) or
                message.endswith(py3_msg)):
                self.assertEqual(level,logging.ERROR)
                self.assertEqual(kw,{'exc_info':True})
                break
        else:
            self.fail("error not in log %s" % log)

        # cleanup
        del conn.logger.log

    def checkConnectionInvalidationOnReconnect(self):

        storage = ClientStorage(self.addr, wait=1, min_disconnect_poll=0.1)
        self._storage = storage

        # and we'll wait for the storage to be reconnected:
        for i in range(100):
            if storage.is_connected():
                break
            time.sleep(0.1)
        else:
            raise AssertionError("Couldn't connect to server")

        class DummyDB:
            _invalidatedCache = 0
            def invalidateCache(self):
                self._invalidatedCache += 1
            def invalidate(*a, **k):
                pass

        db = DummyDB()
        storage.registerDB(db)

        base = db._invalidatedCache

        # Now we'll force a disconnection and reconnection
        storage._connection.close()

        # and we'll wait for the storage to be reconnected:
        for i in range(100):
            if storage.is_connected():
                break
            time.sleep(0.1)
        else:
            raise AssertionError("Couldn't connect to server")

        # Now, the root object in the connection should have been invalidated:
        self.assertEqual(db._invalidatedCache, base+1)


class CommonBlobTests:

    def getConfig(self):
        return """
        <blobstorage 1>
          blob-dir blobs
          <filestorage 2>
            path Data.fs
          </filestorage>
        </blobstorage>
        """

    blobdir = 'blobs'
    blob_cache_dir = 'blob_cache'

    def checkStoreBlob(self):
        import transaction
        from ZODB.blob import Blob
        from ZODB.tests.StorageTestBase import handle_serials
        from ZODB.tests.StorageTestBase import ZERO
        from ZODB.tests.StorageTestBase import zodb_pickle

        somedata = b'a' * 10

        blob = Blob()
        with blob.open('w') as bd_fh:
            bd_fh.write(somedata)
        tfname = bd_fh.name
        oid = self._storage.new_oid()
        data = zodb_pickle(blob)
        self.assert_(os.path.exists(tfname))

        t = transaction.Transaction()
        try:
            self._storage.tpc_begin(t)
            r1 = self._storage.storeBlob(oid, ZERO, data, tfname, '', t)
            r2 = self._storage.tpc_vote(t)
            revid = handle_serials(oid, r1, r2)
            self._storage.tpc_finish(t)
        except:
            self._storage.tpc_abort(t)
            raise
        self.assert_(not os.path.exists(tfname))
        filename = self._storage.fshelper.getBlobFilename(oid, revid)
        self.assert_(os.path.exists(filename))
        with open(filename, 'rb') as f:
            self.assertEqual(somedata, f.read())

    def checkStoreBlob_wrong_partition(self):
        os_rename = os.rename
        try:
            def fail(*a):
                raise OSError
            os.rename = fail
            self.checkStoreBlob()
        finally:
            os.rename = os_rename

    def checkLoadBlob(self):
        from ZODB.blob import Blob
        from ZODB.tests.StorageTestBase import zodb_pickle, ZERO, \
             handle_serials
        import transaction

        somedata = b'a' * 10

        blob = Blob()
        with blob.open('w') as bd_fh:
            bd_fh.write(somedata)
        tfname = bd_fh.name
        oid = self._storage.new_oid()
        data = zodb_pickle(blob)

        t = transaction.Transaction()
        try:
            self._storage.tpc_begin(t)
            r1 = self._storage.storeBlob(oid, ZERO, data, tfname, '', t)
            r2 = self._storage.tpc_vote(t)
            serial = handle_serials(oid, r1, r2)
            self._storage.tpc_finish(t)
        except:
            self._storage.tpc_abort(t)
            raise

        filename = self._storage.loadBlob(oid, serial)
        with open(filename, 'rb') as f:
            self.assertEqual(somedata, f.read())
        self.assert_(not(os.stat(filename).st_mode & stat.S_IWRITE))
        self.assert_((os.stat(filename).st_mode & stat.S_IREAD))

    def checkTemporaryDirectory(self):
        self.assertEquals(os.path.join(self.blob_cache_dir, 'tmp'),
                          self._storage.temporaryDirectory())

    def checkTransactionBufferCleanup(self):
        oid = self._storage.new_oid()
        with open('blob_file', 'wb') as f:
            f.write(b'I am a happy blob.')
        t = transaction.Transaction()
        self._storage.tpc_begin(t)
        self._storage.storeBlob(
          oid, ZODB.utils.z64, 'foo', 'blob_file', '', t)
        self._storage.close()


class BlobAdaptedFileStorageTests(FullGenericTests, CommonBlobTests):
    """ZEO backed by a BlobStorage-adapted FileStorage."""

    def checkStoreAndLoadBlob(self):
        import transaction
        from ZODB.blob import Blob
        from ZODB.tests.StorageTestBase import handle_serials
        from ZODB.tests.StorageTestBase import ZERO
        from ZODB.tests.StorageTestBase import zodb_pickle

        somedata_path = os.path.join(self.blob_cache_dir, 'somedata')
        with open(somedata_path, 'w+b') as somedata:
            for i in range(1000000):
                somedata.write(("%s\n" % i).encode('ascii'))

            def check_data(path):
                self.assert_(os.path.exists(path))
                f = open(path, 'rb')
                somedata.seek(0)
                d1 = d2 = 1
                while d1 or d2:
                    d1 = f.read(8096)
                    d2 = somedata.read(8096)
                    self.assertEqual(d1, d2)
            somedata.seek(0)

            blob = Blob()
            with blob.open('w') as bd_fh:
                ZODB.utils.cp(somedata, bd_fh)
                bd_fh.close()
                tfname = bd_fh.name
            oid = self._storage.new_oid()
            data = zodb_pickle(blob)
            self.assert_(os.path.exists(tfname))

            t = transaction.Transaction()
            try:
                self._storage.tpc_begin(t)
                r1 = self._storage.storeBlob(oid, ZERO, data, tfname, '', t)
                r2 = self._storage.tpc_vote(t)
                revid = handle_serials(oid, r1, r2)
                self._storage.tpc_finish(t)
            except:
                self._storage.tpc_abort(t)
                raise

            # The uncommitted data file should have been removed
            self.assert_(not os.path.exists(tfname))

            # The file should be in the cache ...
            filename = self._storage.fshelper.getBlobFilename(oid, revid)
            check_data(filename)

            # ... and on the server
            server_filename = os.path.join(
                self.blobdir,
                ZODB.blob.BushyLayout().getBlobFilePath(oid, revid),
                )

            self.assert_(server_filename.startswith(self.blobdir))
            check_data(server_filename)

            # If we remove it from the cache and call loadBlob, it should
            # come back. We can do this in many threads.  We'll instrument
            # the method that is used to request data from teh server to
            # verify that it is only called once.

            sendBlob_org = ServerStub.StorageServer.sendBlob
            calls = []
            def sendBlob(self, oid, serial):
                calls.append((oid, serial))
                sendBlob_org(self, oid, serial)

            ZODB.blob.remove_committed(filename)
            returns = []
            threads = [
                threading.Thread(
                target=lambda :
                        returns.append(self._storage.loadBlob(oid, revid))
                )
                for i in range(10)
                ]
            [thread.start() for thread in threads]
            [thread.join() for thread in threads]
            [self.assertEqual(r, filename) for r in returns]
            check_data(filename)


class BlobWritableCacheTests(FullGenericTests, CommonBlobTests):

    blob_cache_dir = 'blobs'
    shared_blob_dir = True

class FauxConn:
    addr = 'x'
    peer_protocol_version = zrpc_connection.Connection.current_protocol

class StorageServerClientWrapper:

    def __init__(self):
        self.serials = []

    def serialnos(self, serials):
        self.serials.extend(serials)

    def info(self, info):
        pass

class StorageServerWrapper:

    def __init__(self, server, storage_id):
        self.storage_id = storage_id
        self.server = StorageServer.ZEOStorage(server, server.read_only)
        self.server.notifyConnected(FauxConn())
        self.server.register(storage_id, False)
        self.server.client = StorageServerClientWrapper()

    def sortKey(self):
        return self.storage_id

    def __getattr__(self, name):
        return getattr(self.server, name)

    def registerDB(self, *args):
        pass

    def supportsUndo(self):
        return False

    def new_oid(self):
        return self.server.new_oids(1)[0]

    def tpc_begin(self, transaction):
        self.server.tpc_begin(id(transaction), '', '', {}, None, ' ')

    def tpc_vote(self, transaction):
        vote_result = self.server.vote(id(transaction))
        assert vote_result is None
        result = self.server.client.serials[:]
        del self.server.client.serials[:]
        return result

    def store(self, oid, serial, data, version_ignored, transaction):
        self.server.storea(oid, serial, data, id(transaction))

    def send_reply(self, *args):        # Masquerade as conn
        pass

    def tpc_abort(self, transaction):
        self.server.tpc_abort(id(transaction))

    def tpc_finish(self, transaction, func = lambda: None):
        self.server.tpc_finish(id(transaction)).set_sender(0, self)


def multiple_storages_invalidation_queue_is_not_insane():
    """
    >>> from ..StorageServer import StorageServer, ZEOStorage
    >>> from ZODB.FileStorage import FileStorage
    >>> from ZODB.DB import DB
    >>> from persistent.mapping import PersistentMapping
    >>> from transaction import commit
    >>> fs1 = FileStorage('t1.fs')
    >>> fs2 = FileStorage('t2.fs')
    >>> server = StorageServer(('', get_port()), dict(fs1=fs1, fs2=fs2))

    >>> s1 = StorageServerWrapper(server, 'fs1')
    >>> s2 = StorageServerWrapper(server, 'fs2')

    >>> db1 = DB(s1); conn1 = db1.open()
    >>> db2 = DB(s2); conn2 = db2.open()

    >>> commit()
    >>> o1 = conn1.root()
    >>> for i in range(10):
    ...     o1.x = PersistentMapping(); o1 = o1.x
    ...     commit()

    >>> last = fs1.lastTransaction()
    >>> for i in range(5):
    ...     o1.x = PersistentMapping(); o1 = o1.x
    ...     commit()

    >>> o2 = conn2.root()
    >>> for i in range(20):
    ...     o2.x = PersistentMapping(); o2 = o2.x
    ...     commit()

    >>> trans, oids = s1.getInvalidations(last)
    >>> from ZODB.utils import u64
    >>> sorted([int(u64(oid)) for oid in oids])
    [10, 11, 12, 13, 14]

    >>> server.close()
    """

def getInvalidationsAfterServerRestart():
    """

Clients were often forced to verify their caches after a server
restart even if there weren't many transactions between the server
restart and the client connect.

Let's create a file storage and stuff some data into it:

    >>> from ..StorageServer import StorageServer, ZEOStorage
    >>> from ZODB.FileStorage import FileStorage
    >>> from ZODB.DB import DB
    >>> from persistent.mapping import PersistentMapping
    >>> fs = FileStorage('t.fs')
    >>> db = DB(fs)
    >>> conn = db.open()
    >>> from transaction import commit
    >>> last = []
    >>> for i in range(100):
    ...     conn.root()[i] = PersistentMapping()
    ...     commit()
    ...     last.append(fs.lastTransaction())
    >>> db.close()

Now we'll open a storage server on the data, simulating a restart:

    >>> fs = FileStorage('t.fs')
    >>> sv = StorageServer(('', get_port()), dict(fs=fs))
    >>> s = ZEOStorage(sv, sv.read_only)
    >>> s.notifyConnected(FauxConn())
    >>> s.register('fs', False)

If we ask for the last transaction, we should get the last transaction
we saved:

    >>> s.lastTransaction() == last[-1]
    True

If a storage implements the method lastInvalidations, as FileStorage
does, then the stroage server will populate its invalidation data
structure using lastTransactions.


    >>> tid, oids = s.getInvalidations(last[-10])
    >>> tid == last[-1]
    True


    >>> from ZODB.utils import u64
    >>> sorted([int(u64(oid)) for oid in oids])
    [0, 92, 93, 94, 95, 96, 97, 98, 99, 100]

(Note that the fact that we get oids for 92-100 is actually an
artifact of the fact that the FileStorage lastInvalidations method
returns all OIDs written by transactions, even if the OIDs were
created and not modified. FileStorages don't record whether objects
were created rather than modified. Objects that are just created don't
need to be invalidated.  This means we'll invalidate objects that
dont' need to be invalidated, however, that's better than verifying
caches.)

    >>> sv.close()
    >>> fs.close()

If a storage doesn't implement lastInvalidations, a client can still
avoid verifying its cache if it was up to date when the server
restarted.  To illustrate this, we'll create a subclass of FileStorage
without this method:

    >>> class FS(FileStorage):
    ...     lastInvalidations = property()

    >>> fs = FS('t.fs')
    >>> sv = StorageServer(('', get_port()), dict(fs=fs))
    >>> st = StorageServerWrapper(sv, 'fs')
    >>> s = st.server

Now, if we ask for the invalidations since the last committed
transaction, we'll get a result:

    >>> tid, oids = s.getInvalidations(last[-1])
    >>> tid == last[-1]
    True
    >>> oids
    []

    >>> db = DB(st); conn = db.open()
    >>> ob = conn.root()
    >>> for i in range(5):
    ...     ob.x = PersistentMapping(); ob = ob.x
    ...     commit()
    ...     last.append(fs.lastTransaction())

    >>> ntid, oids = s.getInvalidations(tid)
    >>> ntid == last[-1]
    True

    >>> sorted([int(u64(oid)) for oid in oids])
    [0, 101, 102, 103, 104]

    >>> fs.close()
    """

def tpc_finish_error():
    r"""Server errors in tpc_finish weren't handled properly.

    >>> class Connection:
    ...     peer_protocol_version = (
    ...         zrpc_connection.Connection.current_protocol)
    ...     def __init__(self, client):
    ...         self.client = client
    ...     def get_addr(self):
    ...         return 'server'
    ...     def is_async(self):
    ...         return True
    ...     def register_object(self, ob):
    ...         pass
    ...     def close(self):
    ...         print('connection closed')
    ...     trigger = property(lambda self: self)
    ...     pull_trigger = lambda self, func, *args: func(*args)

    >>> class ConnectionManager:
    ...     def __init__(self, addr, client, tmin, tmax):
    ...         self.client = client
    ...     def connect(self, sync=1):
    ...         self.client.notifyConnected(Connection(self.client))
    ...     def close(self):
    ...         pass

    >>> class StorageServer:
    ...     should_fail = True
    ...     def __init__(self, conn):
    ...         self.conn = conn
    ...         self.t = None
    ...     def get_info(self):
    ...         return {}
    ...     def endZeoVerify(self):
    ...         self.conn.client.endVerify()
    ...     def lastTransaction(self):
    ...         return b'\0'*8
    ...     def tpc_begin(self, t, *args):
    ...         if self.t is not None:
    ...             raise TypeError('already trans')
    ...         self.t = t
    ...         print('begin', args)
    ...     def vote(self, t):
    ...         if self.t != t:
    ...             raise TypeError('bad trans')
    ...         print('vote')
    ...     def tpc_finish(self, *args):
    ...         if self.should_fail:
    ...             raise TypeError()
    ...         print('finish')
    ...     def tpc_abort(self, t):
    ...         if self.t != t:
    ...             raise TypeError('bad trans')
    ...         self.t = None
    ...         print('abort')
    ...     def iterator_gc(*args):
    ...         pass

    >>> class ClientStorage(ClientStorage):
    ...     ConnectionManagerClass = ConnectionManager
    ...     StorageServerStubClass = StorageServer

    >>> class Transaction:
    ...     user = 'test'
    ...     description = ''
    ...     _extension = {}

    >>> cs = ClientStorage(('', ''))
    >>> t1 = Transaction()
    >>> cs.tpc_begin(t1)
    begin ('test', '', {}, None, ' ')

    >>> cs.tpc_vote(t1)
    vote

    >>> cs.tpc_finish(t1)
    Traceback (most recent call last):
    ...
    TypeError

    >>> cs.tpc_abort(t1)
    abort

    >>> t2 = Transaction()
    >>> cs.tpc_begin(t2)
    begin ('test', '', {}, None, ' ')
    >>> cs.tpc_vote(t2)
    vote

    If client storage has an internal error after the storage finish
    succeeeds, it will close the connection, which will force a
    restart and reverification.

    >>> StorageServer.should_fail = False
    >>> cs._update_cache = lambda : None
    >>> try: cs.tpc_finish(t2)
    ... except: pass
    ... else: print("Should have failed")
    finish
    connection closed

    >>> cs.close()
    """

def client_has_newer_data_than_server():
    """It is bad if a client has newer data than the server.

    >>> db = ZODB.DB('Data.fs')
    >>> db.close()
    >>> r = shutil.copyfile('Data.fs', 'Data.save')
    >>> addr, admin = start_server(keep=1)
    >>> from .. import DB, __name__ as ZEO_name
    >>> db = DB(addr, name='client', max_disconnect_poll=.01)
    >>> wait_connected(db.storage)
    >>> conn = db.open()
    >>> conn.root().x = 1
    >>> transaction.commit()

    OK, we've added some data to the storage and the client cache has
    the new data. Now, we'll stop the server, put back the old data, and
    see what happens. :)

    >>> stop_server(admin)
    >>> r = shutil.copyfile('Data.save', 'Data.fs')

    >>> import zope.testing.loggingsupport
    >>> handler = zope.testing.loggingsupport.InstalledHandler(
    ...     ZEO_name, level=logging.ERROR)
    >>> formatter = logging.Formatter('%(name)s %(levelname)s %(message)s')

    >>> _, admin = start_server(addr=addr)

    >>> wait_until('got enough errors', lambda:
    ...    len([x for x in handler.records
    ...         if x.filename.lower() == 'clientstorage.py' and
    ...            x.funcName == 'verify_cache' and
    ...            x.levelname == 'CRITICAL' and
    ...            x.msg == 'client Client has seen '
    ...                     'newer transactions than server!']) >= 2)

    Note that the errors repeat because the client keeps on trying to connect.

    >>> db.close()
    >>> handler.uninstall()
    >>> stop_server(admin)

    """

def history_over_zeo():
    """
    >>> addr, _ = start_server()
    >>> db = DB(addr)
    >>> wait_connected(db.storage)
    >>> conn = db.open()
    >>> conn.root().x = 0
    >>> transaction.commit()
    >>> len(db.history(conn.root()._p_oid, 99))
    2

    >>> db.close()
    """

def dont_log_poskeyerrors_on_server():
    """
    >>> addr, admin = start_server()
    >>> cs = ClientStorage(addr)
    >>> cs.load(ZODB.utils.p64(1))
    Traceback (most recent call last):
    ...
    POSKeyError: 0x01

    >>> cs.close()
    >>> stop_server(admin)
    >>> with open('server-%s.log' % addr[1]) as f:
    ...     'POSKeyError' in f.read()
    False
    """

def open_convenience():
    """Often, we just want to open a single connection.

    >>> addr, _ = start_server(path='data.fs')
    >>> conn = connection(addr)
    >>> conn.root()
    {}

    >>> conn.root()['x'] = 1
    >>> transaction.commit()
    >>> conn.close()

    Let's make sure the database was cloased when we closed the
    connection, and that the data is there.

    >>> db = DB(addr)
    >>> conn = db.open()
    >>> conn.root()
    {'x': 1}
    >>> db.close()
    """

def client_asyncore_thread_has_name():
    """
    >>> addr, _ = start_server()
    >>> db = DB(addr)
    >>> any(t for t in threading.enumerate()
    ...     if ' zeo client networking thread' in t.getName())
    True
    >>> db.close()
    """

def runzeo_without_configfile():
    """
    >>> with open('runzeo', 'w') as r:
    ...     _ = r.write('''
    ... import sys
    ... sys.path[:] = %r
    ... from .. import runzeo
    ... runzeo.main(sys.argv[1:])
    ... ''' % sys.path)

    >>> import subprocess, re
    >>> print(re.sub(b'\d\d+|[:]', b'', subprocess.Popen(
    ...     [sys.executable, 'runzeo', '-a:%s' % get_port(), '-ft', '--test'],
    ...     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    ...     ).stdout.read()).decode('ascii'))
    ... # doctest: +ELLIPSIS +NORMALIZE_WHITESPACE
    ------
    --T INFO ...runzeo () opening storage '1' using FileStorage
    ------
    --T INFO ...StorageServer StorageServer created RW with storages 1RWt
    ------
    --T INFO ...zrpc () listening on ...
    ------
    --T INFO ...StorageServer closing storage '1'
    testing exit immediately
    """

def close_client_storage_w_invalidations():
    r"""
Invalidations could cause errors when closing client storages,

    >>> addr, _ = start_server()
    >>> writing = threading.Event()
    >>> def mad_write_thread():
    ...     global writing
    ...     conn = connection(addr)
    ...     writing.set()
    ...     while writing.isSet():
    ...         conn.root.x = 1
    ...         transaction.commit()
    ...     conn.close()

    >>> thread = threading.Thread(target=mad_write_thread)
    >>> thread.setDaemon(True)
    >>> thread.start()
    >>> _ = writing.wait()
    >>> time.sleep(.01)
    >>> for i in range(10):
    ...     conn = connection(addr)
    ...     _ = conn._storage.load(b'\0'*8)
    ...     conn.close()

    >>> writing.clear()
    >>> thread.join(1)
    """

def convenient_to_pass_port_to_client_and_ZEO_dot_client():
    """Jim hates typing

    >>> addr, _ = start_server()
    >>> from .. import client
    >>> client = client(addr[1])
    >>> client.__name__ == "('127.0.0.1', %s)" % addr[1]
    True

    >>> client.close()
    """

def test_server_status():
    """
    You can get server status using the server_status method.

    >>> addr, _ = start_server(zeo_conf=dict(transaction_timeout=1))
    >>> db = DB(addr)
    >>> pprint.pprint(db.storage.server_status(), width=40)
    {'aborts': 0,
     'active_txns': 0,
     'commits': 1,
     'conflicts': 0,
     'conflicts_resolved': 0,
     'connections': 1,
     'last-transaction': '03ac11b771fa1c00',
     'loads': 1,
     'lock_time': None,
     'start': 'Tue May  4 10:55:20 2010',
     'stores': 1,
     'timeout-thread-is-alive': True,
     'verifying_clients': 0,
     'waiting': 0}

    >>> db.close()
    """

def test_ruok():
    """
    You can also get server status using the ruok protocol.

    >>> addr, _ = start_server(zeo_conf=dict(transaction_timeout=1))
    >>> db = DB(addr) # force a transaction :)
    >>> import json, socket, struct
    >>> s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    >>> s.connect(addr)
    >>> writer = s.makefile(mode='wb')
    >>> _ = writer.write(struct.pack(">I", 4)+b"ruok")
    >>> writer.close()
    >>> proto = s.recv(struct.unpack(">I", s.recv(4))[0])
    >>> data = json.loads(s.recv(struct.unpack(">I", s.recv(4))[0]).decode("ascii"))
    >>> pprint.pprint(data['1'])
    {u'aborts': 0,
     u'active_txns': 0,
     u'commits': 1,
     u'conflicts': 0,
     u'conflicts_resolved': 0,
     u'connections': 1,
     u'last-transaction': u'03ac11cd11372499',
     u'loads': 1,
     u'lock_time': None,
     u'start': u'Sun Jan  4 09:37:03 2015',
     u'stores': 1,
     u'timeout-thread-is-alive': True,
     u'verifying_clients': 0,
     u'waiting': 0}
    >>> db.close(); s.close()
    """

def client_labels():
    """
When looking at server logs, for servers with lots of clients coming
from the same machine, it can be very difficult to correlate server
log entries with actual clients.  It's possible, sort of, but tedious.

You can make this easier by passing a label to the ClientStorage
constructor.

    >>> addr, _ = start_server()
    >>> db = DB(addr, client_label='test-label-1')
    >>> db.close()
    >>> @wait_until
    ... def check_for_test_label_1():
    ...    with open('server-%s.log' % addr[1]) as f:
    ...        for line in f:
    ...            if 'test-label-1' in line:
    ...                print(line.split()[1:4])
    ...                return True
    ['INFO', 'ZEO.StorageServer', '(test-label-1']

You can specify the client label via a configuration file as well:

    >>> import ZODB.config
    >>> db = ZODB.config.databaseFromString('''
    ... <zodb>
    ...    <zeoclient>
    ...       server :%s
    ...       client-label test-label-2
    ...    </zeoclient>
    ... </zodb>
    ... ''' % addr[1])
    >>> db.close()
    >>> @wait_until
    ... def check_for_test_label_2():
    ...    with open('server-%s.log' % addr[1]) as f:
    ...        for line in open('server-%s.log' % addr[1]):
    ...            if 'test-label-2' in line:
    ...                print(line.split()[1:4])
    ...                return True
    ['INFO', ZEO__name__ + '.StorageServer', '(test-label-2']

    """

def invalidate_client_cache_entry_on_server_commit_error():
    """

When the serials returned during commit includes an error, typically a
conflict error, invalidate the cache entry.  This is important when
the cache is messed up.

    >>> addr, _ = start_server()
    >>> conn1 = connection(addr)
    >>> conn1.root.x = conn1.root().__class__()
    >>> transaction.commit()
    >>> conn1.root.x
    {}

    >>> cs = ClientStorage(addr, client='cache')
    >>> conn2 = ZODB.connection(cs)
    >>> conn2.root.x
    {}

    >>> conn2.close()
    >>> cs.close()

    >>> conn1.root.x['x'] = 1
    >>> transaction.commit()
    >>> conn1.root.x
    {'x': 1}

Now, let's screw up the cache by making it have a last tid that is later than
the root serial.

    >>> from .. import cache as client_cache
    >>> cache = client_cache.ClientCache('cache-1.zec')
    >>> cache.setLastTid(p64(u64(conn1.root.x._p_serial)+1))
    >>> cache.close()

We'll also update the server so that it's last tid is newer than the cache's:

    >>> conn1.root.y = 1
    >>> transaction.commit()
    >>> conn1.root.y = 2
    >>> transaction.commit()

Now, if we reopen the client storage, we'll get the wrong root:

    >>> cs = ClientStorage(addr, client='cache')
    >>> conn2 = ZODB.connection(cs)
    >>> conn2.root.x
    {}

And, we'll get a conflict error if we try to modify it:

    >>> conn2.root.x['y'] = 1
    >>> transaction.commit() # doctest: +ELLIPSIS
    Traceback (most recent call last):
    ...
    ConflictError: ...

But, if we abort, we'll get up to date data and we'll see the changes.

    >>> transaction.abort()
    >>> conn2.root.x
    {'x': 1}
    >>> conn2.root.x['y'] = 1
    >>> transaction.commit()
    >>> sorted(conn2.root.x.items())
    [('x', 1), ('y', 1)]

    >>> conn2.close()
    >>> cs.close()
    >>> conn1.close()
    """


script_template = """
import sys
sys.path[:] = %(path)r

%(src)s

"""
def generate_script(name, src):
    with open(name, 'w') as f:
        f.write(script_template % dict(
            exe=sys.executable,
            path=sys.path,
            src=src,
        ))

def read(filename):
    with open(filename) as f:
        return f.read()

def runzeo_logrotate_on_sigusr2():
    """
    >>> port = get_port()
    >>> with open('c', 'w') as r:
    ...    _ = r.write('''
    ... <zeo>
    ...    address %s
    ... </zeo>
    ... <mappingstorage>
    ... </mappingstorage>
    ... <eventlog>
    ...    <logfile>
    ...       path l
    ...    </logfile>
    ... </eventlog>
    ... ''' % port)
    >>> generate_script('s', '''
    ... from {} import runzeo
    ... runzeo.main()
    ... '''.format(ZEO__name__))
    >>> import subprocess, signal
    >>> p = subprocess.Popen([sys.executable, 's', '-Cc'], close_fds=True)
    >>> wait_until('started',
    ...       lambda : os.path.exists('l') and ('listening on' in read('l'))
    ...     )

    >>> oldlog = read('l')
    >>> os.rename('l', 'o')
    >>> os.kill(p.pid, signal.SIGUSR2)

    >>> wait_until('new file', lambda : os.path.exists('l'))

    XXX: if any of the previous commands failed, we'll hang here trying to
    connect to ZEO, because doctest runs all the assertions...

    >>> s = ClientStorage(port)
    >>> s.close()
    >>> wait_until('See logging', lambda : ('Log files ' in read('l')))
    >>> read('o') == oldlog  # No new data in old log
    True

    # Cleanup:

    >>> os.kill(p.pid, signal.SIGKILL)
    >>> _ = p.wait()
    """

def unix_domain_sockets():
    """Make sure unix domain sockets work

    >>> addr, _ = start_server(port='./sock')

    >>> c = connection(addr)
    >>> c.root.x = 1
    >>> transaction.commit()
    >>> c.close()
    """

def gracefully_handle_abort_while_storing_many_blobs():
    r"""

    >>> import logging, sys
    >>> old_level = logging.getLogger().getEffectiveLevel()
    >>> logging.getLogger().setLevel(logging.ERROR)
    >>> handler = logging.StreamHandler(sys.stdout)
    >>> logging.getLogger().addHandler(handler)

    >>> addr, _ = start_server(blob_dir='blobs')
    >>> from .. import client
    >>> client = client(addr, blob_dir='cblobs')
    >>> c = ZODB.connection(client)
    >>> c.root.x = ZODB.blob.Blob(b'z'*(1<<20))
    >>> c.root.y = ZODB.blob.Blob(b'z'*(1<<2))
    >>> t = c.transaction_manager.get()
    >>> c.tpc_begin(t)
    >>> c.commit(t)

We've called commit, but the blob sends are queued.  We'll call abort
right away, which will delete the temporary blob files.  The queued
iterators will try to open these files.

    >>> c.tpc_abort(t)

Now we'll try to use the connection, mainly to wait for everything to
get processed. Before we fixed this by making tpc_finish a synchronous
call to the server. we'd get some sort of error here.

    >>> _ = client._server.loadEx(b'\0'*8)

    >>> c.close()

    >>> logging.getLogger().removeHandler(handler)
    >>> logging.getLogger().setLevel(old_level)



    """

if sys.platform.startswith('win'):
    del runzeo_logrotate_on_sigusr2
    del unix_domain_sockets

def work_with_multiprocessing_process(name, addr, q):
    conn = connection(addr)
    q.put((name, conn.root.x))
    conn.close()

class MultiprocessingTests(unittest.TestCase):

    layer = ZODB.tests.util.MininalTestLayer('work_with_multiprocessing')

    def test_work_with_multiprocessing(self):
        "Client storage should work with multi-processing."

        # Gaaa, zope.testing.runner.FakeInputContinueGenerator has no close
        if not hasattr(sys.stdin, 'close'):
            sys.stdin.close = lambda : None
        if not hasattr(sys.stdin, 'fileno'):
            sys.stdin.fileno = lambda : -1

        self.globs = {}
        forker.setUp(self)
        addr, adminaddr = self.globs['start_server']()
        conn = connection(addr)
        conn.root.x = 1
        transaction.commit()
        q = multiprocessing.Queue()
        processes = [multiprocessing.Process(
            target=work_with_multiprocessing_process,
            args=(i, addr, q))
                        for i in range(3)]
        _ = [p.start() for p in processes]
        self.assertEqual(sorted(q.get(timeout=300) for p in processes),
                            [(0, 1), (1, 1), (2, 1)])

        _ = [p.join(30) for p in processes]
        conn.close()
        zope.testing.setupstack.tearDown(self)

def quick_close_doesnt_kill_server():
    r"""

    Start a server:

    >>> addr, _ = start_server()

    Now connect and immediately disconnect. This caused the server to
    die in the past:

    >>> import socket, struct
    >>> for i in range(5):
    ...     s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ...     s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
    ...                  struct.pack('ii', 1, 0))
    ...     s.connect(addr)
    ...     s.close()

    Now we should be able to connect as normal:

    >>> db = DB(addr)
    >>> db.storage.is_connected()
    True

    >>> db.close()
    """

def sync_connect_doesnt_hang():
    r"""
    >>> import threading
    >>> ConnectThread = zrpc_client.ConnectThread
    >>> zrpc_client.ConnectThread = lambda *a, **kw: threading.Thread()

    >>> class CM(zrpc_client.ConnectionManager):
    ...     sync_wait = 1
    ...     _start_asyncore_loop = lambda self: None
    >>> cm = CM(('', 0), object())

    Calling connect results in an exception being raised, instead of hanging
    indefinitely when the thread dies without setting up the connection.

    >>> cm.connect(sync=1)
    Traceback (most recent call last):
    ...
    AssertionError

    >>> cm.thread.isAlive()
    False
    >>> zrpc_client.ConnectThread = ConnectThread
    """

def lp143344_extension_methods_not_lost_on_server_restart():
    r"""
Make sure we don't lose exension methods on server restart.

    >>> addr, adminaddr = start_server(keep=True)
    >>> conn = connection(addr)
    >>> conn.root.x = 1
    >>> transaction.commit()
    >>> conn.db().storage.answer_to_the_ultimate_question()
    42

    >>> stop_server(adminaddr)
    >>> wait_until('not connected',
    ...            lambda : not conn.db().storage.is_connected())
    >>> _ = start_server(addr=addr)
    >>> wait_until('connected', conn.db().storage.is_connected)

    >>> conn.root.x
    1
    >>> conn.db().storage.answer_to_the_ultimate_question()
    42

    >>> conn.close()
    """

def can_use_empty_string_for_local_host_on_client():
    """We should be able to spell localhost with ''.

    >>> (_, port), _ = start_server()
    >>> conn = connection(('', port))
    >>> conn.root()
    {}
    >>> conn.root.x = 1
    >>> transaction.commit()

    >>> conn.close()
    """

slow_test_classes = [
    BlobAdaptedFileStorageTests, BlobWritableCacheTests,
    MappingStorageTests, DemoStorageTests,
    FileStorageTests, FileStorageHexTests, FileStorageClientHexTests,
    ]

quick_test_classes = [
    FileStorageRecoveryTests, ConfigurationTests, HeartbeatTests,
    ZRPCConnectionTests,
    ]

class ServerManagingClientStorage(ClientStorage):

    class StorageServerStubClass(ServerStub.StorageServer):

        # Wait for abort for the benefit of blob_transaction.txt
        def tpc_abort(self, id):
            self.rpc.call('tpc_abort', id)

    def __init__(self, name, blob_dir, shared=False, extrafsoptions=''):
        if shared:
            server_blob_dir = blob_dir
        else:
            server_blob_dir = 'server-'+blob_dir
        self.globs = {}
        port = forker.get_port2(self)
        addr, admin, pid, config = forker.start_zeo_server(
            """
            <blobstorage>
                blob-dir %s
                <filestorage>
                   path %s
                   %s
                </filestorage>
            </blobstorage>
            """ % (server_blob_dir, name+'.fs', extrafsoptions),
            port=port,
            )
        os.remove(config)
        zope.testing.setupstack.register(self, os.waitpid, pid, 0)
        zope.testing.setupstack.register(
            self, forker.shutdown_zeo_server, admin)
        if shared:
            ClientStorage.__init__(self, addr, blob_dir=blob_dir,
                                   shared_blob_dir=True)
        else:
            ClientStorage.__init__(self, addr, blob_dir=blob_dir)

    def close(self):
        ClientStorage.close(self)
        zope.testing.setupstack.tearDown(self)

def create_storage_shared(name, blob_dir):
    return ServerManagingClientStorage(name, blob_dir, True)

class ServerManagingClientStorageForIExternalGCTest(
    ServerManagingClientStorage):

    def pack(self, t=None, referencesf=None):
        ServerManagingClientStorage.pack(self, t, referencesf, wait=True)
        # Packing doesn't clear old versions out of zeo client caches,
        # so we'll clear the caches.
        self._cache.clear()
        from ..ClientStorage import _check_blob_cache_size
        _check_blob_cache_size(self.blob_dir, 0)

def test_suite():
    suite = unittest.TestSuite()

    # Collect misc tests into their own layer to reduce size of
    # unit test layer
    zeo = unittest.TestSuite()
    zeo.addTest(unittest.makeSuite(ZODB.tests.util.AAAA_Test_Runner_Hack))
    patterns = [
        (re.compile(r"u?'start': u?'[^\n]+'"), 'start'),
        (re.compile(r"u?'last-transaction': u?'[0-9a-f]+'"),
         'last-transaction'),
        (re.compile(r"\[Errno \d+\]"), '[Errno N]'),
        (re.compile(r"loads=\d+\.\d+"), 'loads=42.42'),
        # Python 3 drops the u prefix
        (re.compile("u('.*?')"), r"\1"),
        (re.compile('u(".*?")'), r"\1")
        ]
    if not PY3:
        patterns.append((re.compile("^'(blob[^']*)'"), r"b'\1'"))
        patterns.append((re.compile("^'Z308'"), "b'Z308'"))
    zeo.addTest(doctest.DocTestSuite(
        setUp=forker.setUp, tearDown=zope.testing.setupstack.tearDown,
        checker=renormalizing.RENormalizing(patterns),
        optionflags=doctest.IGNORE_EXCEPTION_DETAIL,
        ))
    zeo.addTest(doctest.DocTestSuite(
            IterationTests,
            setUp=forker.setUp, tearDown=zope.testing.setupstack.tearDown,
            optionflags=doctest.IGNORE_EXCEPTION_DETAIL,
            ))
    zeo.addTest(doctest.DocFileSuite(
            'registerDB.test', globs={'print_function': print_function}))
    zeo.addTest(
        doctest.DocFileSuite(
            'zeo-fan-out.test', 'zdoptions.test',
            'drop_cache_rather_than_verify.txt', 'client-config.test',
            'protocols.test', 'zeo_blob_cache.test', 'invalidation-age.txt',
            'dynamic_server_ports.test', 'new_addr.test', '../nagios.rst',
            setUp=forker.setUp, tearDown=zope.testing.setupstack.tearDown,
            checker=renormalizing.RENormalizing(patterns),
            optionflags=doctest.IGNORE_EXCEPTION_DETAIL,
            globs={'print_function': print_function},
            ),
        )
    zeo.addTest(PackableStorage.IExternalGC_suite(
        lambda :
        ServerManagingClientStorageForIExternalGCTest(
            'data.fs', 'blobs', extrafsoptions='pack-gc false')
        ))
    for klass in quick_test_classes:
        zeo.addTest(unittest.makeSuite(klass, "check"))
    zeo.layer = ZODB.tests.util.MininalTestLayer('testZeo-misc')
    suite.addTest(zeo)

    suite.addTest(unittest.makeSuite(MultiprocessingTests))

    # Put the heavyweights in their own layers
    for klass in slow_test_classes:
        sub = unittest.makeSuite(klass, "check")
        sub.layer = ZODB.tests.util.MininalTestLayer(klass.__name__)
        suite.addTest(sub)

    suite.addTest(ZODB.tests.testblob.storage_reusable_suite(
        'ClientStorageNonSharedBlobs', ServerManagingClientStorage))
    suite.addTest(ZODB.tests.testblob.storage_reusable_suite(
        'ClientStorageSharedBlobs', create_storage_shared))

    return suite


if __name__ == "__main__":
    unittest.main(defaultTest="test_suite")
