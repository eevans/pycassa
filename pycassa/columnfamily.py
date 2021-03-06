"""
Provides an abstraction of Cassandra's data model to allow for easy
manipulation of data inside Cassandra.

.. seealso:: :mod:`pycassa.columnfamilymap`
"""

from pycassa.cassandra.ttypes import Column, ColumnOrSuperColumn,\
    ColumnParent, ColumnPath, ConsistencyLevel, NotFoundException,\
    SlicePredicate, SliceRange, SuperColumn, KeyRange,\
    IndexExpression, IndexClause
from pycassa.util import *

import time
import sys
import uuid
import struct

from batch import CfMutator

__all__ = ['gm_timestamp', 'ColumnFamily']

_TYPES = ['BytesType', 'LongType', 'IntegerType', 'UTF8Type', 'AsciiType',
         'LexicalUUIDType', 'TimeUUIDType']

_NON_SLICE = 0
_SLICE_START = 1
_SLICE_FINISH = 2

def gm_timestamp():
    """
    Gets the current GMT timestamp

    :Returns:
        integer UNIX epoch time in GMT

    """
    return int(time.time() * 1e6)

def create_SlicePredicate(columns, column_start, column_finish, column_reversed, column_count):
    if columns is not None:
        return SlicePredicate(column_names=columns)
    sr = SliceRange(start=column_start, finish=column_finish,
                    reversed=column_reversed, count=column_count)
    return SlicePredicate(slice_range=sr)


class ColumnFamily(object):
    """An abstraction of a Cassandra column family or super column family."""

    def __init__(self, client, column_family, buffer_size=1024,
                 read_consistency_level=ConsistencyLevel.ONE,
                 write_consistency_level=ConsistencyLevel.ONE,
                 timestamp=gm_timestamp, super=False,
                 dict_class=dict, autopack_names=True,
                 autopack_values=True):
        """
        Constructs an abstraction of a Cassandra column family or super column family.

        Operations on this, such as `get` or `insert` will get data from or
        insert data into the corresponding Cassandra column family.

        :Parameters:
            `client`: :class:`cassandra.Cassandra.Client`
                Cassandra client with thrift API
            `column_family`: string
                The name of this ColumnFamily
            `buffer_size`: integer
                When calling `get_range`, the intermediate results need to be
                buffered if we are fetching many rows, otherwise the Cassandra
                server will overallocate memory and fail.  This is the size of
                that buffer.
            `read_consistency_level`: :class:`pycassa.cassandra.ttypes.ConsistencyLevel`
                Affects the guaranteed replication factor before returning from
                any read operation
            `write_consistency_level`: :class:`pycassa.cassandra.ttypes.ConsistencyLevel`
                Affects the guaranteed replication factor before returning from
                any write operation
            `timestamp`: function
                The default timestamp function returns:
                int(time.mktime(time.gmtime()))
                Or the number of seconds since Unix epoch in GMT.
                Set timestamp to replace the default timestamp function with your
                own.
            `super`: bool
                Whether this ColumnFamily has SuperColumns
            `dict_class` : class (must act like the dict type)
                The default dict_class is :class:`dict`.
                If the order of columns matter to you, pass your own dictionary
                class, or python 2.7's new :class:`collections.OrderedDict`. All returned
                rows and subcolumns are instances of this.
            `autopack_names`: bool
                Whether column and supercolumn names should be packed automatically
                based on the comparator and subcomparator for the column
                family.  This does not typically work when used with
                :class:`~pycassa.columnfamilymap.ColumnFamilyMap`.
            `autopack_values`: bool
                Whether column values should be packed automatically based on
                the validator_class for a given column.  This should probably
                be set to ``False`` when used with a
                :class:`~pycassa.columnfamilymap.ColumnFamilyMap`.

        """

        self.client = client
        self.column_family = column_family
        self.buffer_size = buffer_size
        self.read_consistency_level = read_consistency_level
        self.write_consistency_level = write_consistency_level
        self.timestamp = timestamp
        self.super = super
        self.dict_class = dict_class
        self.autopack_names = autopack_names
        self.autopack_values = autopack_values

        # Determine the ColumnFamily type to allow for auto conversion
        # so that packing/unpacking doesn't need to be done manually
        self.cf_data_type = None
        self.col_name_data_type = None
        self.supercol_name_data_type = None
        self.col_type_dict = dict()

        col_fam = None
        try:
            col_fam = client.get_keyspace_description()[self.column_family]
        except KeyError:
            raise NotFoundException('Column family %s not found.' % self.column_family)

        if col_fam is not None:
            if self.autopack_names:
                if not self.super:
                    self.col_name_data_type = col_fam.comparator_type
                else:
                    self.col_name_data_type = col_fam.subcomparator_type
                    self.supercol_name_data_type = self._extract_type_name(col_fam.comparator_type)

                index = self.col_name_data_type = self._extract_type_name(self.col_name_data_type)
            if self.autopack_values:
                self.cf_data_type = self._extract_type_name(col_fam.default_validation_class)
                for name, cdef in col_fam.column_metadata.items():
                    self.col_type_dict[name] = self._extract_type_name(cdef.validation_class)


    def _extract_type_name(self, string):

        if string is None: return 'BytesType'

        index = string.rfind('.')
        if index == -1:
            string = 'BytesType'
        else:
            string = string[index + 1: ]
            if string not in _TYPES:
                string = 'BytesType'
        return string

    def _convert_Column_to_base(self, column, include_timestamp):
        value = self._unpack_value(column.value, column.name)
        if include_timestamp:
            return (value, column.timestamp)
        return value

    def _convert_SuperColumn_to_base(self, super_column, include_timestamp):
        ret = self.dict_class()
        for column in super_column.columns:
            ret[self._unpack_name(column.name)] = self._convert_Column_to_base(column, include_timestamp)
        return ret

    def _convert_ColumnOrSuperColumns_to_dict_class(self, list_col_or_super, include_timestamp):
        ret = self.dict_class()
        for col_or_super in list_col_or_super:
            if col_or_super.super_column is not None:
                col = col_or_super.super_column
                ret[self._unpack_name(col.name, is_supercol_name=True)] = self._convert_SuperColumn_to_base(col, include_timestamp)
            else:
                col = col_or_super.column
                ret[self._unpack_name(col.name)] = self._convert_Column_to_base(col, include_timestamp)
        return ret

    def _convert_KeySlice_list_to_dict_class(self, keyslice_list, include_timestamp):
        ret = self.dict_class()
        for keyslice in keyslice_list:
            ret[keyslice.key] = self._convert_ColumnOrSuperColumns_to_dict_class(keyslice.columns, include_timestamp)
        return ret

    def _rcl(self, alternative):
        """Helper function that returns self.read_consistency_level if
        alternative is None, otherwise returns alternative"""
        if alternative is None:
            return self.read_consistency_level
        return alternative

    def _wcl(self, alternative):
        """Helper function that returns self.write_consistency_level
        if alternative is None, otherwise returns alternative"""
        if alternative is None:
            return self.write_consistency_level
        return alternative

    def _pack_slice_cols(self, super_column, column_start, column_finish):
        if super_column != '':
            super_column = self._pack_name(super_column, is_supercol_name=True)
        if column_start != '':
            column_start = self._pack_name(column_start,
                                           is_supercol_name=self.super,
                                           slice_end=_SLICE_START)
        if column_finish != '':
            column_finish = self._pack_name(column_finish,
                                            is_supercol_name=self.super,
                                            slice_end=_SLICE_FINISH)
        return super_column, column_start, column_finish

    def _pack_name(self, value, is_supercol_name=False,
            slice_end=_NON_SLICE):
        if not self.autopack_names:
            return value
        if value is None: return

        if is_supercol_name:
            d_type = self.supercol_name_data_type
        else:
            d_type = self.col_name_data_type

        if d_type == 'TimeUUIDType':
            if slice_end:
                value = convert_time_to_uuid(value,
                        lowest_val=(slice_end == _SLICE_START),
                        randomize=False)
            else:
                value = convert_time_to_uuid(value,
                        randomize=True)

        return self._pack(value, d_type)

    def _unpack_name(self, b, is_supercol_name=False):
        if not self.autopack_names:
            return b
        if b is None: return

        if is_supercol_name:
            d_type = self.supercol_name_data_type
        else:
            d_type = self.col_name_data_type

        return self._unpack(b, d_type)

    def _get_data_type_for_col(self, col_name):
        if col_name not in self.col_type_dict.keys(): 
            return self.cf_data_type
        return self.col_type_dict[col_name]

    def _pack_value(self, value, col_name):
        if not self.autopack_values:
            return value
        return self._pack(value, self._get_data_type_for_col(col_name))

    def _unpack_value(self, value, col_name):
        if not self.autopack_values:
            return value
        return self._unpack(value, self._get_data_type_for_col(col_name))

    def _pack(self, value, data_type):
        """
        Packs a value into the expected sequence of bytes that Cassandra expects.
        """
        if data_type == 'LongType':
            return struct.pack('>q', long(value))  # q is 'long long'
        elif data_type == 'IntegerType':
            return struct.pack('>i', int(value))
        elif data_type == 'AsciiType':
            return struct.pack(">%ds" % len(value), value)
        elif data_type == 'UTF8Type':
            try:
                st = value.encode('utf-8')
            except UnicodeDecodeError:
                # value is already utf-8 encoded
                st = value
            return struct.pack(">%ds" % len(st), st)
        elif data_type == 'TimeUUIDType' or data_type == 'LexicalUUIDType':
            if not hasattr(value, 'bytes'):
                raise TypeError("%s not valid for %s" % (value, data_type))
            return struct.pack('>16s', value.bytes)
        else:
            return value

    def _unpack(self, b, data_type):
        """
        Unpacks Cassandra's byte-representation of values into their Python
        equivalents.
        """

        if data_type == 'LongType':
            return struct.unpack('>q', b)[0]
        elif data_type == 'IntegerType':
            return struct.unpack('>i', b)[0]
        elif data_type == 'AsciiType':
            return struct.unpack('>%ds' % len(b), b)[0]
        elif data_type == 'UTF8Type':
            unic = struct.unpack('>%ds' % len(b), b)[0]
            return unic.decode('utf-8')
        elif data_type == 'LexicalUUIDType' or data_type == 'TimeUUIDType':
            temp_bytes = struct.unpack('>16s', b)[0]
            return uuid.UUID(bytes=temp_bytes)
        else: # BytesType
            return b

    def get(self, key, columns=None, column_start="", column_finish="",
            column_reversed=False, column_count=100, include_timestamp=False,
            super_column=None, read_consistency_level = None):
        """
        Fetch a key from a Cassandra server

        :Parameters:
            `key`: str
                The key to fetch
            `columns`: [str]
                Limit the columns or super_columns fetched to the specified list
            `column_start`: str
                Only fetch when a column or super_column is >= column_start
            `column_finish`: str
                Only fetch when a column or super_column is <= column_finish
            `column_reversed`: bool
                Fetch the columns or super_columns in reverse order. This will do
                nothing unless you passed a ``dict_class`` to the constructor.
            `column_count`: int
                Limit the number of columns or super_columns fetched per key
            `include_timestamp` : bool
                If true, return a (value, timestamp) tuple for each column
            `super_column`: str
                Return columns only in this super_column
            `read_consistency_level`: :class:`pycassa.cassandra.ttypes.ConsistencyLevel`
                Affects the guaranteed replication factor before returning from
                any read operation

        :Returns:
            if include_timestamp == True: {'column': ('value', timestamp)}
            else: {'column': 'value'}

        """

        super_column, column_start, column_finish = self._pack_slice_cols(
                super_column, column_start, column_finish)

        packed_cols = None
        if columns is not None:
            packed_cols = []
            for col in columns:
                packed_cols.append(self._pack_name(col, is_supercol_name=self.super))

        cp = ColumnParent(column_family=self.column_family, super_column=super_column)
        sp = create_SlicePredicate(packed_cols, column_start, column_finish,
                                   column_reversed, column_count)

        list_col_or_super = self.client.get_slice(key, cp, sp,
                                                  self._rcl(read_consistency_level))

        if len(list_col_or_super) == 0:
            raise NotFoundException()
        return self._convert_ColumnOrSuperColumns_to_dict_class(list_col_or_super, include_timestamp)

    def get_indexed_slices(self, index_clause, columns=None, column_start="", column_finish="",
                          column_reversed=False, column_count=100, include_timestamp=False,
                          super_column=None, read_consistency_level=None):
        """
        Fetches a list of KeySlices from a Cassandra server based on an index clause

        :Parameters:
            `index_clause`: :class:`~pycassa.cassandra.ttypes.IndexClause`
                Limits the keys that are returned based on expressions that compare
                the value of a column to a given value.  At least one of the
                expressions in the IndexClause must be on an indexed column.
                .. seealso:: meth::pycassa.index.create_index_clause() and
                             meth::pycassa.index.create_index_expression().
            `columns`: [str]
                Limit the columns or super_columns fetched to the specified list
            `column_start`: str
                Only fetch when a column or super_column is >= column_start
            `column_finish`: str
                Only fetch when a column or super_column is <= column_finish
            `column_reversed`: bool
                Fetch the columns or super_columns in reverse order. This will do
                nothing unless you passed a dict_class to the constructor.
            `column_count`: int
                Limit the number of columns or super_columns fetched per key
            `include_timestamp`: bool
                If true, return a (value, timestamp) tuple for each column
            `super_column`: str
                Return columns only in this super_column
            `read_consistency_level`: :class:`pycassa.cassandra.ttypes.ConsistencyLevel`
                Affects the guaranteed replication factor before returning from
                any read operation

        :Returns:
            if include_timestamp == True: {key : {column : (value, timestamp)}}
            else: {key : {column : value}}
        """

        (super_column, column_start, column_finish) = self._pack_slice_cols(
                super_column, column_start, column_finish)

        packed_cols = None
        if columns is not None:
            packed_cols = []
            for col in columns:
                packed_cols.append(self._pack_name(col, is_supercol_name=self.super))

        cp = ColumnParent(column_family=self.column_family, super_column=super_column)
        sp = create_SlicePredicate(packed_cols, column_start, column_finish,
                                   column_reversed, column_count)

        # Pack the values in the index clause expressions
        new_exprs = []
        for expr in index_clause.expressions:
            new_exprs.append(IndexExpression(self._pack_name(expr.column_name), expr.op, \
                            self._pack_value(expr.value, expr.column_name)))
        index_clause.expressions = new_exprs

        keyslice_list = self.client.get_indexed_slices(cp, index_clause, sp,
                                                  self._rcl(read_consistency_level))

        if len(keyslice_list) == 0:
            raise NotFoundException()
        return self._convert_KeySlice_list_to_dict_class(keyslice_list, include_timestamp)

    def multiget(self, keys, columns=None, column_start="", column_finish="",
                 column_reversed=False, column_count=100, include_timestamp=False,
                 super_column=None, read_consistency_level = None):
        """
        Fetch multiple keys from a Cassandra server

        :Parameters:
            `keys`: [str]
                A list of keys to fetch
            `columns`: [str]
                Limit the columns or super_columns fetched to the specified list
            `column_start`: str
                Only fetch when a column or super_column is >= column_start
            `column_finish`: str
                Only fetch when a column or super_column is <= column_finish
            `column_reversed`: bool
                Fetch the columns or super_columns in reverse order. This will do
                nothing unless you passed a dict_class to the constructor.
            `column_count`: int
                Limit the number of columns or super_columns fetched per key
            `include_timestamp`: bool
                If true, return a (value, timestamp) tuple for each column
            `super_column`: str
                Return columns only in this super_column
            `read_consistency_level`: :class:`pycassa.cassandra.ttypes.ConsistencyLevel`
                Affects the guaranteed replication factor before returning from
                any read operation

        :Returns:
            if include_timestamp == True: {'key': {'column': ('value', timestamp)}}
            else: {'key': {'column': 'value'}}
        """

        (super_column, column_start, column_finish) = self._pack_slice_cols(
                super_column, column_start, column_finish)

        packed_cols = None
        if columns is not None:
            packed_cols = []
            for col in columns:
                packed_cols.append(self._pack_name(col, is_supercol_name=self.super))

        cp = ColumnParent(column_family=self.column_family, super_column=super_column)
        sp = create_SlicePredicate(packed_cols, column_start, column_finish,
                                   column_reversed, column_count)

        keymap = self.client.multiget_slice(keys, cp, sp,
                                            self._rcl(read_consistency_level))
        
        ret = self.dict_class()
        
        # Keep the order of keys
        for key in keys:
            ret[key] = None

        non_empty_keys = []
        for key, columns in keymap.iteritems():
            if len(columns) > 0:
                non_empty_keys.append(key)
                ret[key] = self._convert_ColumnOrSuperColumns_to_dict_class(columns, include_timestamp) 

        for key in keys:
            if key not in non_empty_keys:
                del ret[key]

        return ret

    MAX_COUNT = 2**31-1
    def get_count(self, key, super_column=None,
                  read_consistency_level=None,
                  columns=None, column_start="",
                  column_finish=""):
        """
        Count the number of columns for a key

        :Parameters:
            `key`: str
                The key to count columns for
            `columns`: [str]
                Limit the columns or super_columns counted to the specified list
            `column_start`: str
                Only count when a column or super_column is >= column_start
            `column_finish`: str
                Only count when a column or super_column is <= column_finish
            `super_column` : str
                Count the columns only in this super_column
            `read_consistency_level` : :class:`pycassa.cassandra.ttypes.ConsistencyLevel`
                Affects the guaranteed replication factor before returning from
                any read operation

        :Returns:
            int Count of columns
        """

        (super_column, column_start, column_finish) = self._pack_slice_cols(
                super_column, column_start, column_finish)

        packed_cols = None
        if columns is not None:
            packed_cols = []
            for col in columns:
                packed_cols.append(self._pack_name(col, is_supercol_name=self.super))

        cp = ColumnParent(column_family=self.column_family, super_column=super_column)
        sp = create_SlicePredicate(packed_cols, column_start, column_finish,
                                   False, self.MAX_COUNT)


        return self.client.get_count(key, cp, sp,
                                     self._rcl(read_consistency_level))

    def multiget_count(self, keys, super_column=None,
                       read_consistency_level=None,
                       columns=None, column_start="",
                       column_finish="", ):
        """
        Perform a get_count in parallel on a list of keys.

        :Parameters:
            `keys` : [str]
                The keys to count columns for
            `columns`: [str]
                Limit the columns or super_columns counted to the specified list
            `column_start`: str
                Only count when a column or super_column is >= column_start
            `column_finish`: str
                Only count when a column or super_column is <= column_finish
            `super_column` : str
                Count the columns only in this super_column
            `read_consistency_level` : :class:`pycassa.cassandra.ttypes.ConsistencyLevel`
                Affects the guaranteed replication factor before returning from
                any read operation

        :Returns:
            {'keyname': int count}
        """

        (super_column, column_start, column_finish) = self._pack_slice_cols(
                super_column, column_start, column_finish)

        packed_cols = None
        if columns is not None:
            packed_cols = []
            for col in columns:
                packed_cols.append(self._pack_name(col, is_supercol_name=self.super))

        cp = ColumnParent(column_family=self.column_family, super_column=super_column)
        sp = create_SlicePredicate(packed_cols, column_start, column_finish,
                                   False, self.MAX_COUNT)

        return self.client.multiget_count(keys, cp, sp,
                                     self._rcl(read_consistency_level))


    def get_range(self, start="", finish="", columns=None, column_start="",
                  column_finish="", column_reversed=False, column_count=100,
                  row_count=None, include_timestamp=False,
                  super_column=None, read_consistency_level = None):
        """
        Get an iterator over keys in a specified range

        :Parameters:
            `start`: str
                Start from this key (inclusive)
            `finish`: str
                End at this key (inclusive)
            `columns`: [str]
                Limit the columns or super_columns fetched to the specified list
            `column_start`: str
                Only fetch when a column or super_column is >= column_start
            `column_finish`: str
                Only fetch when a column or super_column is <= column_finish
            `column_reversed`: bool
                Fetch the columns or super_columns in reverse order. This will do
                nothing unless you passed a dict_class to the constructor.
            `column_count`: int
                Limit the number of columns or super_columns fetched per key
            `row_count`: int
                Limit the number of rows fetched
            `include_timestamp`: bool
                If true, return a (value, timestamp) tuple for each column
            `super_column`: string
                Return columns only in this super_column
            `read_consistency_level`: :class:`pycassa.cassandra.ttypes.ConsistencyLevel`
                Affects the guaranteed replication factor before returning from
                any read operation

        :Returns:
            iterator over ('key', {'column': 'value'})
        """

        (super_column, column_start, column_finish) = self._pack_slice_cols(
                super_column, column_start, column_finish)

        packed_cols = None
        if columns is not None:
            packed_cols = []
            for col in columns:
                packed_cols.append(self._pack_name(col, is_supercol_name=self.super))

        cp = ColumnParent(column_family=self.column_family, super_column=super_column)
        sp = create_SlicePredicate(packed_cols, column_start, column_finish,
                                   column_reversed, column_count)

        count = 0
        i = 0
        last_key = start

        buffer_size = self.buffer_size
        if row_count is not None:
            buffer_size = min(row_count, self.buffer_size)
        while True:
            key_range = KeyRange(start_key=last_key, end_key=finish, count=buffer_size)
            key_slices = self.client.get_range_slices(cp, sp, key_range,
                                                     self._rcl(read_consistency_level))
            # This may happen if nothing was ever inserted
            if key_slices is None:
                return
            for j, key_slice in enumerate(key_slices):
                # Ignore the first element after the first iteration
                # because it will be a duplicate.
                if j == 0 and i != 0:
                    continue
                yield (key_slice.key,
                       self._convert_ColumnOrSuperColumns_to_dict_class(key_slice.columns, include_timestamp))
                count += 1
                if row_count is not None and count >= row_count:
                    return

            if len(key_slices) != self.buffer_size:
                return
            last_key = key_slices[-1].key
            i += 1

    def insert(self, key, columns, timestamp=None, ttl=None,
               write_consistency_level=None):
        """
        Insert or update columns for a key

        :Parameters:
            `key`: str
                The key to insert or update the columns at
            `columns`: dict
                Column: {'column': 'value'}
                SuperColumn: {'column': {'subcolumn': 'value'}}
                The columns or supercolumns to insert or update
            `write_consistency_level`: :class:`pycassa.cassandra.ttypes.ConsistencyLevel`
                Affects the guaranteed replication factor before returning from
                any write operation

        :Returns:
            int timestamp
        """
        return self.batch_insert({key: columns}, timestamp=timestamp, ttl=ttl,
                                 write_consistency_level=write_consistency_level)

    def batch_insert(self, rows, timestamp=None, ttl=None, write_consistency_level = None):
        """
        Insert or update columns for multiple keys

        :Parameters:
            `rows`: :class:`dict`
                Column: {'row': {'column': 'value'}}
                SuperColumn: {'row': {'column': {'subcolumn': 'value'}}}
            `write_consistency_level`: :class:`pycassa.cassandra.ttypes.ConsistencyLevel`
                Affects the guaranteed replication factor before returning from
                any write operation

        :Returns:
            int timestamp
        """
        if timestamp == None:
            timestamp = self.timestamp()
        batch = self.batch(write_consistency_level=write_consistency_level)
        for key, columns in rows.iteritems():
            batch.insert(key, columns, timestamp=timestamp, ttl=ttl)
        batch.send()
        return timestamp

    def remove(self, key, columns=None, super_column=None, write_consistency_level = None):
        """
        Remove a specified key or columns

        :Parameters:
            `key`: str
                The key to remove. If columns is not set, remove all columns
            `columns`: list
                Delete the columns or super_columns in this list
            `super_column`: str
                Delete the columns from this super_column
            `write_consistency_level`: :class:`pycassa.cassandra.ttypes.ConsistencyLevel`
                Affects the guaranteed replication factor before returning from
                any write operation

        :Returns:
            int timestamp
        """
        timestamp = self.timestamp()
        batch = self.batch(write_consistency_level=write_consistency_level)
        batch.remove(key, columns, super_column, timestamp)
        batch.send()
        return timestamp

    def batch(self, queue_size=100, write_consistency_level=None):
        """
        Create batch mutator for doing multiple insert, update, and remove
        operations using as few roundtrips as possible.

        :Parameters:
            `queue_size`: int
                Max number of mutations per request
            `write_consistency_level`: :class:`pycassa.cassandra.ttypes.ConsistencyLevel`
                Consistency level used for mutations.

        :Returns:
            :class:`pycassa.batch.CfMutator`
        """
        if write_consistency_level is None:
            write_consistency_level = self.write_consistency_level
        return CfMutator(self, queue_size=queue_size,
                         write_consistency_level=write_consistency_level)

    def truncate(self):
        """
        Marks the entire ColumnFamily as deleted.

        From the user's perspective a successful call to truncate will result
        complete data deletion from cfname. Internally, however, disk space
        will not be immediatily released, as with all deletes in cassandra,
        this one only marks the data as deleted.

        The operation succeeds only if all hosts in the cluster at available
        and will throw an :exc:`.UnavailableException` if some hosts are
        down.

        """
        self.client.truncate(self.column_family)
