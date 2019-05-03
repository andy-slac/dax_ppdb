# This file is part of dax_ppdb.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (http://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Module responsible for PPDB schema operations.
"""

__all__ = ["PpdbSqlSchema", "PpdbSqlSchemaConfig"]

import logging

from lsst.pex.config import Field, ChoiceField
import sqlalchemy
from sqlalchemy import (Column, Index, MetaData, PrimaryKeyConstraint,
                        UniqueConstraint, Table)
from sqlalchemy.schema import CreateTable, CreateIndex
from sqlalchemy.ext.compiler import compiles
from .ppdbBaseSchema import PpdbBaseSchema, PpdbBaseSchemaConfig

_LOG = logging.getLogger(__name__.partition(".")[2])  # strip leading "lsst."


@compiles(CreateTable, "oracle")
def _add_suffixes_tbl(element, compiler, **kw):
    """Add all needed suffixed for Oracle CREATE TABLE statement.

    This is a special compilation method for CreateTable clause which
    registers itself with SQLAlchemy using @compiles decotrator. Exact method
    name does not matter. Client can pass a dict to ``info`` keyword argument
    of Table constructor. If the dict has a key "oracle_tablespace" then its
    value is used as tablespace name. If the dict has a key "oracle_iot" with
    true value then IOT table is created. This method generates additional
    clauses for CREATE TABLE statement which specify tablespace name and
    "ORGANIZATION INDEX" for IOT.

    .. seealso:: https://docs.sqlalchemy.org/en/latest/core/compiler.html
    """
    text = compiler.visit_create_table(element, **kw)
    _LOG.debug("text: %r", text)
    oracle_tablespace = element.element.info.get("oracle_tablespace")
    oracle_iot = element.element.info.get("oracle_iot", False)
    _LOG.debug("oracle_tablespace: %r", oracle_tablespace)
    if oracle_iot:
        text += " ORGANIZATION INDEX"
    if oracle_tablespace:
        text += " TABLESPACE " + oracle_tablespace
    _LOG.debug("text: %r", text)
    return text


@compiles(CreateIndex, "oracle")
def _add_suffixes_idx(element, compiler, **kw):
    """Add all needed suffixed for Oracle CREATE INDEX statement.

    This is a special compilation method for CreateIndex clause which
    registers itself with SQLAlchemy using @compiles decotrator. Exact method
    name does not matter. Client can pass a dict to ``info`` keyword argument
    of Index constructor. If the dict has a key "oracle_tablespace" then its
    value is used as tablespace name. This method generates additional
    clause for CREATE INDEX statement which specifies tablespace name.

    .. seealso:: https://docs.sqlalchemy.org/en/latest/core/compiler.html
    """
    text = compiler.visit_create_index(element, **kw)
    _LOG.debug("text: %r", text)
    oracle_tablespace = element.element.info.get("oracle_tablespace")
    _LOG.debug("oracle_tablespace: %r", oracle_tablespace)
    if oracle_tablespace:
        text += " TABLESPACE " + oracle_tablespace
    _LOG.debug("text: %r", text)
    return text


class PpdbSqlSchemaConfig(PpdbBaseSchemaConfig):

    dia_object_index = ChoiceField(dtype=str,
                                   doc="Indexing mode for DiaObject table",
                                   allowed={'baseline': "Index defined in baseline schema",
                                            'pix_id_iov': "(pixelId, objectId, iovStart) PK",
                                            'last_object_table': "Separate DiaObjectLast table"},
                                   default='baseline')
    dia_object_nightly = Field(dtype=bool,
                               doc="Use separate nightly table for DiaObject",
                               default=False)
    prefix = Field(dtype=str,
                   doc="Prefix to add to table names and index names",
                   default="")


class PpdbSqlSchema(PpdbBaseSchema):
    """Class for management of PPDB schema.

    Attributes
    ----------
    objects : `sqlalchemy.Table`
        DiaObject table instance
    objects_nightly : `sqlalchemy.Table`
        DiaObjectNightly table instance, may be None
    objects_last : `sqlalchemy.Table`
        DiaObjectLast table instance, may be None
    sources : `sqlalchemy.Table`
        DiaSource table instance
    forcedSources : `sqlalchemy.Table`
        DiaForcedSource table instance
    visits : `sqlalchemy.Table`
        PpdbProtoVisits table instance

    Parameters
    ----------
    engine : `sqlalchemy.engine.Engine`
        SQLAlchemy engine instance
    config : `PpdbSqlSchemaConfig`
        Configuration for this class.
    afw_schemas : `dict`, optional
        Dictionary with table name for a key and `afw.table.Schema`
        for a value. Columns in schema will be added to standard PPDB
        schema (only if standard schema does not have matching column).
    """

    def __init__(self, engine, config, afw_schemas=None):

        super().__init__(config, afw_schemas)

        self._engine = engine
        self._dia_object_index = config.dia_object_index
        self._dia_object_nightly = config.dia_object_nightly
        self._prefix = config.prefix

        self._metadata = MetaData(self._engine)

        self.objects = None
        self.objects_nightly = None
        self.objects_last = None
        self.sources = None
        self.forcedSources = None
        self.visits = None

        # map cat column types to alchemy
        self._type_map = dict(DOUBLE=self._getDoubleType(),
                              FLOAT=sqlalchemy.types.Float,
                              DATETIME=sqlalchemy.types.TIMESTAMP,
                              BIGINT=sqlalchemy.types.BigInteger,
                              INTEGER=sqlalchemy.types.Integer,
                              INT=sqlalchemy.types.Integer,
                              TINYINT=sqlalchemy.types.Integer,
                              BLOB=sqlalchemy.types.LargeBinary,
                              CHAR=sqlalchemy.types.CHAR,
                              BOOL=sqlalchemy.types.Boolean)

        # generate schema for all tables, must be called last
        self._makeTables()

    def _makeTables(self, mysql_engine='InnoDB', oracle_tablespace=None, oracle_iot=False):
        """Generate schema for all tables.

        Parameters
        ----------
        mysql_engine : `str`, optional
            MySQL engine type to use for new tables.
        oracle_tablespace : `str`, optional
            Name of Oracle tablespace, only useful with oracle
        oracle_iot : `bool`, optional
            Make Index-organized DiaObjectLast table.
        """

        info = dict(oracle_tablespace=oracle_tablespace)

        if self._dia_object_index == 'pix_id_iov':
            # Special PK with HTM column in first position
            constraints = self._tableIndices('DiaObjectIndexHtmFirst', info)
        else:
            constraints = self._tableIndices('DiaObject', info)
        table = Table(self._prefix+'DiaObject', self._metadata,
                      *(self._tableColumns('DiaObject') + constraints),
                      mysql_engine=mysql_engine,
                      info=info)
        self.objects = table

        if self._dia_object_nightly:
            # Same as DiaObject but no index
            table = Table(self._prefix+'DiaObjectNightly', self._metadata,
                          *self._tableColumns('DiaObject'),
                          mysql_engine=mysql_engine,
                          info=info)
            self.objects_nightly = table

        if self._dia_object_index == 'last_object_table':
            # Same as DiaObject but with special index
            info2 = info.copy()
            info2.update(oracle_iot=oracle_iot)
            table = Table(self._prefix+'DiaObjectLast', self._metadata,
                          *(self._tableColumns('DiaObjectLast') +
                            self._tableIndices('DiaObjectLast', info)),
                          mysql_engine=mysql_engine,
                          info=info2)
            self.objects_last = table

        # for all other tables use index definitions in schema
        for table_name in ('DiaSource', 'SSObject', 'DiaForcedSource', 'DiaObject_To_Object_Match'):
            table = Table(self._prefix+table_name, self._metadata,
                          *(self._tableColumns(table_name) +
                            self._tableIndices(table_name, info)),
                          mysql_engine=mysql_engine,
                          info=info)
            if table_name == 'DiaSource':
                self.sources = table
            elif table_name == 'DiaForcedSource':
                self.forcedSources = table

        # special table to track visits, only used by prototype
        table = Table(self._prefix+'PpdbProtoVisits', self._metadata,
                      Column('visitId', sqlalchemy.types.BigInteger, nullable=False),
                      Column('visitTime', sqlalchemy.types.TIMESTAMP, nullable=False),
                      PrimaryKeyConstraint('visitId', name=self._prefix+'PK_PpdbProtoVisits'),
                      Index(self._prefix+'IDX_PpdbProtoVisits_vTime', 'visitTime', info=info),
                      mysql_engine=mysql_engine,
                      info=info)
        self.visits = table

    def makeSchema(self, drop=False, mysql_engine='InnoDB', oracle_tablespace=None, oracle_iot=False):
        """Create or re-create all tables.

        Parameters
        ----------
        drop : `bool`, optional
            If True then drop tables before creating new ones.
        mysql_engine : `str`, optional
            MySQL engine type to use for new tables.
        oracle_tablespace : `str`, optional
            Name of Oracle tablespace, only useful with oracle
        oracle_iot : `bool`, optional
            Make Index-organized DiaObjectLast table.
        """

        # re-make table schema for all needed tables with possibly different options
        _LOG.debug("clear metadata")
        self._metadata.clear()
        _LOG.debug("re-do schema mysql_engine=%r oracle_tablespace=%r",
                   mysql_engine, oracle_tablespace)
        self._makeTables(mysql_engine=mysql_engine, oracle_tablespace=oracle_tablespace,
                         oracle_iot=oracle_iot)

        # create all tables (optionally drop first)
        if drop:
            _LOG.info('dropping all tables')
            self._metadata.drop_all()
        _LOG.info('creating all tables')
        self._metadata.create_all()

    def _tableColumns(self, table_name):
        """Return set of columns in a table

        Parameters
        ----------
        table_name : `str`
            Name of the table.

        Returns
        -------
        column_defs : `list`
            List of `Column` objects.
        """

        # get the list of columns in primary key, they are treated somewhat
        # specially below
        table_schema = self.tableSchemas[table_name]
        pkey_columns = set()
        for index in table_schema.indices:
            if index.type == 'PRIMARY':
                pkey_columns = set(index.columns)
                break

        # convert all column dicts into alchemy Columns
        column_defs = []
        for column in table_schema.columns:
            kwargs = dict(nullable=column.nullable)
            if column.default is not None:
                kwargs.update(server_default=str(column.default))
            if column.name in pkey_columns:
                kwargs.update(autoincrement=False)
            ctype = self._type_map[column.type]
            column_defs.append(Column(column.name, ctype, **kwargs))

        return column_defs

    def _tableIndices(self, table_name, info):
        """Return set of constraints/indices in a table

        Parameters
        ----------
        table_name : `str`
            Name of the table.
        info : `dict`
            Additional options passed to SQLAlchemy index constructor.

        Returns
        -------
        index_defs : `list`
            List of SQLAlchemy index/constraint objects.
        """

        table_schema = self.tableSchemas[table_name]

        # convert all index dicts into alchemy Columns
        index_defs = []
        for index in table_schema.indices:
            if index.type == "INDEX":
                index_defs.append(Index(self._prefix+index.name, *index.columns, info=info))
            else:
                kwargs = {}
                if index.name:
                    kwargs['name'] = self._prefix+index.name
                if index.type == "PRIMARY":
                    index_defs.append(PrimaryKeyConstraint(*index.columns, **kwargs))
                elif index.type == "UNIQUE":
                    index_defs.append(UniqueConstraint(*index.columns, **kwargs))

        return index_defs

    def _getDoubleType(self):
        """DOUBLE type is database-specific, select one based on dialect.

        Returns
        -------
        type_object : `object`
            Database-specific type definition.
        """
        if self._engine.name == 'mysql':
            from sqlalchemy.dialects.mysql import DOUBLE
            return DOUBLE(asdecimal=False)
        elif self._engine.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION
            return DOUBLE_PRECISION
        elif self._engine.name == 'oracle':
            from sqlalchemy.dialects.oracle import DOUBLE_PRECISION
            return DOUBLE_PRECISION
        elif self._engine.name == 'sqlite':
            # all floats in sqlite are 8-byte
            from sqlalchemy.dialects.sqlite import REAL
            return REAL
        else:
            raise TypeError('cannot determine DOUBLE type, unexpected dialect: ' + self._engine.name)