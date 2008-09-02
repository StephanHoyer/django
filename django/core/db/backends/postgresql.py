"""
PostgreSQL database backend for Django.

Requires psycopg 1: http://initd.org/projects/psycopg1
"""

from django.core.db import base, typecasts
import psycopg as Database

DatabaseError = Database.DatabaseError

try:
    # Only exists in python 2.4+
    from threading import local
except ImportError:
    # Import copy of _thread_local.py from python 2.4
    from django.utils._threading_local import local

def smart_basestring(s, charset):
    if isinstance(s, unicode):
        return s.encode(charset)
    return s

class UnicodeCursorWrapper(object):
    """
    A thin wrapper around psycopg cursors that allows them to accept Unicode
    strings as params.

    This is necessary because psycopg doesn't apply any DB quoting to
    parameters that are Unicode strings. If a param is Unicode, this will
    convert it to a bytestring using DEFAULT_CHARSET before passing it to
    psycopg.
    """
    def __init__(self, cursor, charset):
        self.cursor = cursor
        self.charset = charset

    def execute(self, sql, params=()):
        try:
            params = dict([(k, smart_basestring(v, self.charset)) for (k, v) in params.items()])
        except AttributeError:
            params = [smart_basestring(p, self.charset) for p in params]
        return self.cursor.execute(sql, params)

    def executemany(self, sql, param_list):
        try:
            new_param_list = [dict([(k, smart_basestring(v, self.charset)) for (k, v) in params.items()])
                              for params in param_list]
        except AttributeError:
            new_param_list = [tuple([smart_basestring(p, self.charset) for p in params])
                              for params in param_list]
        return self.cursor.executemany(sql, new_param_list)

    def __getattr__(self, attr):
        if self.__dict__.has_key(attr):
            return self.__dict__[attr]
        else:
            return getattr(self.cursor, attr)

class DatabaseWrapper(local):
    def __init__(self):
        self.connection = None
        self.queries = []

    def cursor(self):
        from django.conf.settings import DATABASE_USER, DATABASE_NAME, DATABASE_HOST, DATABASE_PORT, DATABASE_PASSWORD, DEBUG, DEFAULT_CHARSET, TIME_ZONE
        if self.connection is None:
            if DATABASE_NAME == '':
                from django.core.exceptions import ImproperlyConfigured
                raise ImproperlyConfigured, "You need to specify DATABASE_NAME in your Django settings file."
            conn_string = "dbname=%s" % DATABASE_NAME
            if DATABASE_USER:
                conn_string = "user=%s %s" % (DATABASE_USER, conn_string)
            if DATABASE_PASSWORD:
                conn_string += " password='%s'" % DATABASE_PASSWORD
            if DATABASE_HOST:
                conn_string += " host=%s" % DATABASE_HOST
            if DATABASE_PORT:
                conn_string += " port=%s" % DATABASE_PORT
            self.connection = Database.connect(conn_string)
            self.connection.set_isolation_level(1) # make transactions transparent to all cursors
        cursor = self.connection.cursor()
        cursor.execute("SET TIME ZONE %s", [TIME_ZONE])
        cursor = UnicodeCursorWrapper(cursor, DEFAULT_CHARSET)
        if DEBUG:
            return base.CursorDebugWrapper(cursor, self)
        return cursor

    def commit(self):
        return self.connection.commit()

    def rollback(self):
        if self.connection:
            return self.connection.rollback()

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def quote_name(self, name):
        if name.startswith('"') and name.endswith('"'):
            return name # Quoting once is enough.
        return '"%s"' % name

def dictfetchone(cursor):
    "Returns a row from the cursor as a dict"
    return cursor.dictfetchone()

def dictfetchmany(cursor, number):
    "Returns a certain number of rows from a cursor as a dict"
    return cursor.dictfetchmany(number)

def dictfetchall(cursor):
    "Returns all rows from a cursor as a dict"
    return cursor.dictfetchall()

def get_last_insert_id(cursor, table_name, pk_name):
    cursor.execute("SELECT CURRVAL('\"%s_%s_seq\"')" % (table_name, pk_name))
    return cursor.fetchone()[0]

def get_date_extract_sql(lookup_type, table_name):
    # lookup_type is 'year', 'month', 'day'
    # http://www.postgresql.org/docs/8.0/static/functions-datetime.html#FUNCTIONS-DATETIME-EXTRACT
    return "EXTRACT('%s' FROM %s)" % (lookup_type, table_name)

def get_date_trunc_sql(lookup_type, field_name):
    # lookup_type is 'year', 'month', 'day'
    # http://www.postgresql.org/docs/8.0/static/functions-datetime.html#FUNCTIONS-DATETIME-TRUNC
    return "DATE_TRUNC('%s', %s)" % (lookup_type, field_name)

def get_limit_offset_sql(limit, offset=None):
    sql = "LIMIT %s" % limit
    if offset and offset != 0:
        sql += " OFFSET %s" % offset
    return sql

def get_random_function_sql():
    return "RANDOM()"

def get_table_list(cursor):
    "Returns a list of table names in the current database."
    cursor.execute("""
        SELECT c.relname
        FROM pg_catalog.pg_class c
        LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'v', '')
            AND n.nspname NOT IN ('pg_catalog', 'pg_toast')
            AND pg_catalog.pg_table_is_visible(c.oid)""")
    return [row[0] for row in cursor.fetchall()]

def get_table_description(cursor, table_name):
    "Returns a description of the table, with the DB-API cursor.description interface."
    cursor.execute("SELECT * FROM %s LIMIT 1" % DatabaseWrapper().quote_name(table_name))
    return cursor.description

def get_relations(cursor, table_name):
    """
    Returns a dictionary of {field_index: (field_index_other_table, other_table)}
    representing all relationships to the given table. Indexes are 0-based.
    """
    cursor.execute("""
        SELECT con.conkey, con.confkey, c2.relname
        FROM pg_constraint con, pg_class c1, pg_class c2
        WHERE c1.oid = con.conrelid
            AND c2.oid = con.confrelid
            AND c1.relname = %s
            AND con.contype = 'f'""", [table_name])
    relations = {}
    for row in cursor.fetchall():
        try:
            # row[0] and row[1] are like "{2}", so strip the curly braces.
            relations[int(row[0][1:-1]) - 1] = (int(row[1][1:-1]) - 1, row[2])
        except ValueError:
            continue
    return relations

def get_indexes(cursor, table_name):
    """
    Returns a dictionary of fieldname -> infodict for the given table,
    where each infodict is in the format:
        {'primary_key': boolean representing whether it's the primary key,
         'unique': boolean representing whether it's a unique index}
    """
    # Get the table description because we only have the column indexes, and we
    # need the column names.
    desc = get_table_description(cursor, table_name)
    # This query retrieves each index on the given table.
    cursor.execute("""
        SELECT idx.indkey, idx.indisunique, idx.indisprimary
        FROM pg_catalog.pg_class c, pg_catalog.pg_class c2,
            pg_catalog.pg_index idx
        WHERE c.oid = idx.indrelid
            AND idx.indexrelid = c2.oid
            AND c.relname = %s""", [table_name])
    indexes = {}
    for row in cursor.fetchall():
        # row[0] (idx.indkey) is stored in the DB as an array. It comes out as
        # a string of space-separated integers. This designates the field
        # indexes (1-based) of the fields that have indexes on the table.
        # Here, we skip any indexes across multiple fields.
        if ' ' in row[0]:
            continue
        col_name = desc[int(row[0])-1][0]
        indexes[col_name] = {'primary_key': row[2], 'unique': row[1]}
    return indexes

# Register these custom typecasts, because Django expects dates/times to be
# in Python's native (standard-library) datetime/time format, whereas psycopg
# use mx.DateTime by default.
try:
    Database.register_type(Database.new_type((1082,), "DATE", typecasts.typecast_date))
except AttributeError:
    raise Exception, "You appear to be using psycopg version 2, which isn't supported yet, because it's still in beta. Use psycopg version 1 instead: http://initd.org/projects/psycopg1"
Database.register_type(Database.new_type((1083,1266), "TIME", typecasts.typecast_time))
Database.register_type(Database.new_type((1114,1184), "TIMESTAMP", typecasts.typecast_timestamp))
Database.register_type(Database.new_type((16,), "BOOLEAN", typecasts.typecast_boolean))

OPERATOR_MAPPING = {
    'exact': '= %s',
    'iexact': 'ILIKE %s',
    'contains': 'LIKE %s',
    'icontains': 'ILIKE %s',
    'ne': '!= %s',
    'gt': '> %s',
    'gte': '>= %s',
    'lt': '< %s',
    'lte': '<= %s',
    'startswith': 'LIKE %s',
    'endswith': 'LIKE %s',
    'istartswith': 'ILIKE %s',
    'iendswith': 'ILIKE %s',
}

# This dictionary maps Field objects to their associated PostgreSQL column
# types, as strings. Column-type strings can contain format strings; they'll
# be interpolated against the values of Field.__dict__ before being output.
# If a column type is set to None, it won't be included in the output.
DATA_TYPES = {
    'AutoField':         'serial',
    'BooleanField':      'boolean',
    'CharField':         'varchar(%(maxlength)s)',
    'CommaSeparatedIntegerField': 'varchar(%(maxlength)s)',
    'DateField':         'date',
    'DateTimeField':     'timestamp with time zone',
    'FileField':         'varchar(100)',
    'FilePathField':     'varchar(100)',
    'FloatField':        'numeric(%(max_digits)s, %(decimal_places)s)',
    'ImageField':        'varchar(100)',
    'IntegerField':      'integer',
    'IPAddressField':    'inet',
    'ManyToManyField':   None,
    'NullBooleanField':  'boolean',
    'OneToOneField':     'integer',
    'PhoneNumberField':  'varchar(20)',
    'PositiveIntegerField': 'integer CHECK ("%(column)s" >= 0)',
    'PositiveSmallIntegerField': 'smallint CHECK ("%(column)s" >= 0)',
    'SlugField':         'varchar(%(maxlength)s)',
    'SmallIntegerField': 'smallint',
    'TextField':         'text',
    'TimeField':         'time',
    'URLField':          'varchar(200)',
    'USStateField':      'varchar(2)',
}

# Maps type codes to Django Field types.
DATA_TYPES_REVERSE = {
    16: 'BooleanField',
    21: 'SmallIntegerField',
    23: 'IntegerField',
    25: 'TextField',
    869: 'IPAddressField',
    1043: 'CharField',
    1082: 'DateField',
    1083: 'TimeField',
    1114: 'DateTimeField',
    1184: 'DateTimeField',
    1266: 'TimeField',
    1700: 'FloatField',
}