import contextlib
import threading
from contextlib import contextmanager

from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from mongoengine.base.fields import _no_dereference_for_fields
from mongoengine.common import _import_class
from mongoengine.connection import (
    DEFAULT_CONNECTION_NAME,
    get_connection,
    get_db,
)
from mongoengine.pymongo_support import count_documents
from mongoengine.sessions import (
    clear_local_session,
    set_local_session,
)

__all__ = (
    "run_in_transaction",
    "switch_db",
    "switch_collection",
    "no_dereference",
    "no_sub_classes",
    "query_counter",
    "set_write_concern",
    "set_read_write_concern",
    "no_dereferencing_active_for_class",
)


class MyThreadLocals(threading.local):
    def __init__(self):
        # {DocCls: count} keeping track of classes with an active no_dereference context
        self.no_dereferencing_class = {}


thread_locals = MyThreadLocals()


def no_dereferencing_active_for_class(cls):
    return cls in thread_locals.no_dereferencing_class


def _register_no_dereferencing_for_class(cls):
    thread_locals.no_dereferencing_class.setdefault(cls, 0)
    thread_locals.no_dereferencing_class[cls] += 1


def _unregister_no_dereferencing_for_class(cls):
    thread_locals.no_dereferencing_class[cls] -= 1
    if thread_locals.no_dereferencing_class[cls] == 0:
        thread_locals.no_dereferencing_class.pop(cls)


class switch_db:
    """switch_db alias context manager.

    Example ::

        # Register connections
        register_connection('default', 'mongoenginetest')
        register_connection('testdb-1', 'mongoenginetest2')

        class Group(Document):
            name = StringField()

        Group(name='test').save()  # Saves in the default db

        with switch_db(Group, 'testdb-1') as Group:
            Group(name='hello testdb!').save()  # Saves in testdb-1
    """

    def __init__(self, cls, db_alias):
        """Construct the switch_db context manager

        :param cls: the class to change the registered db
        :param db_alias: the name of the specific database to use
        """
        self.cls = cls
        self.collection = cls._get_collection()
        self.db_alias = db_alias
        self.ori_db_alias = cls._meta.get("db_alias", DEFAULT_CONNECTION_NAME)

    def __enter__(self):
        """Change the db_alias and clear the cached collection."""
        self.cls._meta["db_alias"] = self.db_alias
        self.cls._collection = None
        return self.cls

    def __exit__(self, t, value, traceback):
        """Reset the db_alias and collection."""
        self.cls._meta["db_alias"] = self.ori_db_alias
        self.cls._collection = self.collection


class switch_collection:
    """switch_collection alias context manager.

    Example ::

        class Group(Document):
            name = StringField()

        Group(name='test').save()  # Saves in the default db

        with switch_collection(Group, 'group1') as Group:
            Group(name='hello testdb!').save()  # Saves in group1 collection
    """

    def __init__(self, cls, collection_name):
        """Construct the switch_collection context manager.

        :param cls: the class to change the registered db
        :param collection_name: the name of the collection to use
        """
        self.cls = cls
        self.ori_collection = cls._get_collection()
        self.ori_get_collection_name = cls._get_collection_name
        self.collection_name = collection_name

    def __enter__(self):
        """Change the _get_collection_name and clear the cached collection."""

        @classmethod
        def _get_collection_name(cls):
            return self.collection_name

        self.cls._get_collection_name = _get_collection_name
        self.cls._collection = None
        return self.cls

    def __exit__(self, t, value, traceback):
        """Reset the collection."""
        self.cls._collection = self.ori_collection
        self.cls._get_collection_name = self.ori_get_collection_name


@contextlib.contextmanager
def no_dereference(cls):
    """no_dereference context manager.

    Turns off all dereferencing in Documents for the duration of the context
    manager::

        with no_dereference(Group):
            Group.objects()
    """
    try:
        cls = cls

        ReferenceField = _import_class("ReferenceField")
        GenericReferenceField = _import_class("GenericReferenceField")
        ComplexBaseField = _import_class("ComplexBaseField")

        deref_fields = [
            field
            for name, field in cls._fields.items()
            if isinstance(
                field, (ReferenceField, GenericReferenceField, ComplexBaseField)
            )
        ]

        _register_no_dereferencing_for_class(cls)

        with _no_dereference_for_fields(*deref_fields):
            yield None
    finally:
        _unregister_no_dereferencing_for_class(cls)


class no_sub_classes:
    """no_sub_classes context manager.

    Only returns instances of this class and no sub (inherited) classes::

        with no_sub_classes(Group) as Group:
            Group.objects.find()
    """

    def __init__(self, cls):
        """Construct the no_sub_classes context manager.

        :param cls: the class to turn querying subclasses on
        """
        self.cls = cls
        self.cls_initial_subclasses = None

    def __enter__(self):
        """Change the objects default and _auto_dereference values."""
        self.cls_initial_subclasses = self.cls._subclasses
        self.cls._subclasses = (self.cls._class_name,)
        return self.cls

    def __exit__(self, t, value, traceback):
        """Reset the default and _auto_dereference values."""
        self.cls._subclasses = self.cls_initial_subclasses


class query_counter:
    """Query_counter context manager to get the number of queries.
    This works by updating the `profiling_level` of the database so that all queries get logged,
    resetting the db.system.profile collection at the beginning of the context and counting the new entries.

    This was designed for debugging purpose. In fact it is a global counter so queries issued by other threads/processes
    can interfere with it

    Usage:

    .. code-block:: python

        class User(Document):
            name = StringField()

        with query_counter() as q:
            user = User(name='Bob')
            assert q == 0       # no query fired yet
            user.save()
            assert q == 1       # 1 query was fired, an 'insert'
            user_bis = User.objects().first()
            assert q == 2       # a 2nd query was fired, a 'find_one'

    Be aware that:

    - Iterating over large amount of documents (>101) makes pymongo issue `getmore` queries to fetch the next batch of documents (https://www.mongodb.com/docs/manual/tutorial/iterate-a-cursor/#cursor-batches)
    - Some queries are ignored by default by the counter (killcursors, db.system.indexes)
    """

    def __init__(self, alias=DEFAULT_CONNECTION_NAME):
        self.db = get_db(alias=alias)
        self.initial_profiling_level = None
        self._ctx_query_counter = 0  # number of queries issued by the context

        self._ignored_query = {
            "ns": {"$ne": "%s.system.indexes" % self.db.name},
            "op": {"$ne": "killcursors"},  # MONGODB < 3.2
            "command.killCursors": {"$exists": False},  # MONGODB >= 3.2
        }

    def _turn_on_profiling(self):
        profile_update_res = self.db.command({"profile": 0})
        self.initial_profiling_level = profile_update_res["was"]

        self.db.system.profile.drop()
        self.db.command({"profile": 2})

    def _resets_profiling(self):
        self.db.command({"profile": self.initial_profiling_level})

    def __enter__(self):
        self._turn_on_profiling()
        return self

    def __exit__(self, t, value, traceback):
        self._resets_profiling()

    def __eq__(self, value):
        counter = self._get_count()
        return value == counter

    def __ne__(self, value):
        return not self.__eq__(value)

    def __lt__(self, value):
        return self._get_count() < value

    def __le__(self, value):
        return self._get_count() <= value

    def __gt__(self, value):
        return self._get_count() > value

    def __ge__(self, value):
        return self._get_count() >= value

    def __int__(self):
        return self._get_count()

    def __repr__(self):
        """repr query_counter as the number of queries."""
        return "%s" % self._get_count()

    def _get_count(self):
        """Get the number of queries by counting the current number of entries in db.system.profile
        and substracting the queries issued by this context. In fact everytime this is called, 1 query is
        issued so we need to balance that
        """
        count = (
            count_documents(self.db.system.profile, self._ignored_query)
            - self._ctx_query_counter
        )
        self._ctx_query_counter += (
            1  # Account for the query we just issued to gather the information
        )
        return count


@contextmanager
def set_write_concern(collection, write_concerns):
    combined_concerns = dict(collection.write_concern.document.items())
    combined_concerns.update(write_concerns)
    yield collection.with_options(write_concern=WriteConcern(**combined_concerns))


@contextmanager
def set_read_write_concern(collection, write_concerns, read_concerns):
    combined_write_concerns = dict(collection.write_concern.document.items())

    if write_concerns is not None:
        combined_write_concerns.update(write_concerns)

    combined_read_concerns = dict(collection.read_concern.document.items())

    if read_concerns is not None:
        combined_read_concerns.update(read_concerns)

    yield collection.with_options(
        write_concern=WriteConcern(**combined_write_concerns),
        read_concern=ReadConcern(**combined_read_concerns),
    )


class run_in_transaction(contextlib.ContextDecorator, contextlib.ExitStack):
    """run_in_transaction alias context manager.

    Example ::

        # Register connections
        register_connection('default', 'mongoenginetest')
        register_connection('testdb-1', 'mongoenginetest2')

        class Group(Document):
            name = StringField()

        Group(name='test').save()  # Saves in the default db without transaction

        with run_in_transaction('testdb-1') as session:
            Group(name='hello testdb!').save()  # Saves in testdb-1 using the provided transaction
    """

    def __init__(self, db_alias=DEFAULT_CONNECTION_NAME):
        """Construct the run_in_transaction context manager

        :param db_alias: the name of the specific database to use
        """
        super().__init__()
        self.db_alias = db_alias

    def __enter__(self):
        """Get a connection and first start a session and then a transaction, the session is stored in the local.
        The session is returned to be used in low level APIs
        """
        super().__enter__()
        self.conn = get_connection(self.db_alias)
        self.session = self.enter_context(self.conn.start_session())
        self.transaction = self.enter_context(self.session.start_transaction())
        set_local_session(self.db_alias, self.session)
        return self.session

    def __exit__(self, t, value, traceback):
        """Clear local session and finish transaction and session"""
        clear_local_session(self.db_alias)
        super().__exit__(t, value, traceback)
