#! /usr/bin/env python
# -*- coding:utf-8 -*-
"""
@author  : MG
@Time    : 2018/4/8 21:11
@File    : db_utils.py
@contact : mmmaaaggg@163.com
@desc    : 数据库相关工具
"""
import pandas as pd
import numpy as np
from sqlalchemy import MetaData, Table
from sqlalchemy.orm.session import Session
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import DeclarativeMeta
import json
from datetime import date, datetime, timedelta
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import Insert
from tasks.utils.fh_utils import date_2_str
import logging

logger = logging.getLogger()


class SessionWrapper:
    """用于对session对象进行封装，方便使用with语句进行close控制"""

    def __init__(self, session):
        self.session = session

    def __enter__(self) -> Session:
        return self.session

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.session.close()
        # logger.debug('db session closed')


def with_db_session(engine, expire_on_commit=True):
    """创建session对象，返回 session_wrapper 可以使用with语句进行调用"""
    db_session = sessionmaker(bind=engine, expire_on_commit=expire_on_commit)
    session = db_session()
    return SessionWrapper(session)


def get_db_session(engine, expire_on_commit=True):
    """返回 session 对象"""
    db_session = sessionmaker(bind=engine, expire_on_commit=expire_on_commit)
    session = db_session()
    return session


class AlchemyEncoder(json.JSONEncoder):
    def default(self, obj):
        # print("obj.__class__", obj.__class__, "isinstance(obj.__class__, DeclarativeMeta)", isinstance(obj.__class__, DeclarativeMeta))
        if isinstance(obj.__class__, DeclarativeMeta):
            # an SQLAlchemy class
            fields = {}
            for field in [x for x in dir(obj) if not x.startswith('_') and x != 'metadata']:
                data = obj.__getattribute__(field)
                try:
                    json.dumps(data)  # this will fail on non-encodable values, like other classes
                    fields[field] = data
                except TypeError:  # 添加了对datetime的处理
                    print(data)
                    if isinstance(data, datetime):
                        fields[field] = data.isoformat()
                    elif isinstance(data, date):
                        fields[field] = data.isoformat()
                    elif isinstance(data, timedelta):
                        fields[field] = (datetime.min + data).time().isoformat()
                    else:
                        fields[field] = None
            # a json-encodable dict
            return fields
        elif isinstance(obj, date):
            return json.dumps(date_2_str(obj))

        return json.JSONEncoder.default(self, obj)


@compiles(Insert)
def append_string(insert, compiler, **kw):
    """
    支持 ON DUPLICATE KEY UPDATE
    通过使用 on_duplicate_key_update=True 开启
    :param insert:
    :param compiler:
    :param kw:
    :return:
    """
    s = compiler.visit_insert(insert, **kw)
    if insert.kwargs.get('on_duplicate_key_update'):
        fields = s[s.find("(") + 1:s.find(")")].replace(" ", "").split(",")
        generated_directive = ["{0}=VALUES({0})".format(field) for field in fields]
        return s + " ON DUPLICATE KEY UPDATE " + ",".join(generated_directive)
    return s


def alter_table_2_myisam(engine, table_name_list=None):
    """
    修改表默认 engine 为 myisam
    :param engine:
    :param table_name_list:
    :return:
    """
    if table_name_list is None:
        table_name_list = engine.table_names()
    with with_db_session(engine=engine) as session:
        data_count = len(table_name_list)
        for num, table_name in enumerate(table_name_list):
            # sql_str = "show table status from {Config.DB_SCHEMA_MD} where name=:table_name"
            row_data = session.execute('show table status like :table_name', params={'table_name': table_name}).first()
            if row_data is None:
                continue
            if row_data[1].lower() == 'myisam':
                continue

            logger.info('%d/%d)修改 %s 表引擎为 MyISAM', num, data_count, table_name)
            sql_str = "ALTER TABLE %s ENGINE = MyISAM" % table_name
            session.execute(sql_str)
            session.commit()


def add_col_2_table(engine, table_name, col_name, col_type_str):
    """
    检查当前数据库是否存在 db_col_name 列，如果不存在则添加该列
    :param engine:
    :param table_name:
    :param col_name:
    :param col_type_str: DOUBLE, VARCHAR(20), INTEGER, etc.
    :return:
    """
    # sql_str = """SELECT * FROM INFORMATION_SCHEMA.COLUMNS
    #     WHERE table_name=:table_name and column_name=:column_name"""
    metadata = MetaData(bind=engine)
    table_model = Table(table_name, metadata, autoload=True)
    if col_name not in table_model.columns:
        # 该语句无法自动更新数据库表结构，因此该方案放弃
        # table_model.append_column(Column(col_name, dtype))
        after_col_name = table_model.columns.keys()[-1]
        add_col_sql_str = "ALTER TABLE `{0}` ADD COLUMN `{1}` {2} NULL AFTER `{3}`".format(
            table_name, col_name, col_type_str, after_col_name
        )
        with with_db_session(engine) as session:
            session.execute(add_col_sql_str)
            session.commit()
        logger.info('%s 添加 %s [%s] 列成功', table_name, col_name, col_type_str)


def bunch_insert_on_duplicate_update(df: pd.DataFrame, table_name, engine, dtype=None, ignore_none=True,
                                     myisam_if_create_table=False, primary_keys: list = None, schema=None):
    """
    将 DataFrame 数据批量插入数据库，ON DUPLICATE KEY UPDATE
    :param df:
    :param table_name:
    :param engine:
    :param dtype: 仅在表不存在的情况下自动创建使用
    :param ignore_none: 为 None 或 NaN 字段不更新
    :param myisam_if_create_table: 如果数据库表为新建，则自动将表engine变为 MYISAM
    :param primary_keys: 如果数据库表为新建，则设置主键为对应list中的key
    :param schema: 仅当需要设置主键时使用
    :return:
    """
    if df is None or df.shape[0] == 0:
        return 0

    has_table = engine.has_table(table_name)
    if has_table:
        col_name_list = list(df.columns)
        if ignore_none:
            generated_directive = ["`{0}`=IFNULL(VALUES(`{0}`), `{0}`)".format(col_name) for col_name in col_name_list]
        else:
            generated_directive = ["`{0}`=VALUES(`{0}`)".format(col_name) for col_name in col_name_list]

        sql_str = "insert into {table_name}({col_names}) VALUES({params}) ON DUPLICATE KEY UPDATE {update}".format(
            table_name=table_name,
            col_names="`" + "`,`".join(col_name_list) + "`",
            params=','.join([':' + col_name for col_name in col_name_list]),
            update=','.join(generated_directive),
        )
        data_dic_list = df.to_dict('records')
        for data_dic in data_dic_list:
            for k, v in data_dic.items():
                if pd.isnull(v):
                    data_dic[k] = None
        with with_db_session(engine) as session:
            rslt = session.execute(sql_str, params=data_dic_list)
            insert_count = rslt.rowcount
            session.commit()
    else:
        df.to_sql(table_name, engine, if_exists='append', index=False, dtype=dtype)
        insert_count = df.shape[0]
        # 修改表engine
        if myisam_if_create_table:
            logger.info('修改 %s 表引擎为 MyISAM', table_name)
            sql_str = f"ALTER TABLE {table_name} ENGINE = MyISAM"
            execute_sql(engine, sql_str, commit=True)
        # 创建主键
        if primary_keys is not None:
            if schema is None:
                raise ValueError('schema 不能为 None，对表设置主键时需要指定schema')
            qry_column_type = """SELECT column_name, column_type
                FROM information_schema.columns 
                WHERE table_schema=:schema AND table_name=:table_name"""
            with with_db_session(engine) as session:
                table = session.execute(qry_column_type, params={'schema': schema, 'table_name': table_name})
                column_type_dic = dict(table.fetchall())
                praimary_keys_len, col_name_last, col_name_sql_str_list = len(primary_keys), None, []
                for num, col_name in enumerate(primary_keys):
                    col_type = column_type_dic[col_name]
                    position_str = 'FIRST' if col_name_last is None else f'AFTER `{col_name_last}`'
                    col_name_sql_str_list.append(
                        f'CHANGE COLUMN `{col_name}` `{col_name}` {col_type} NOT NULL {position_str}' +
                        ("," if num < praimary_keys_len - 1 else "")
                    )
                    col_name_last = col_name
                # chg_pk_str = """ALTER TABLE {table_name}
                #     CHANGE COLUMN `ths_code` `ths_code` VARCHAR(20) NOT NULL FIRST,
                #     CHANGE COLUMN `time` `time` DATE NOT NULL AFTER `ths_code`,
                #     ADD PRIMARY KEY (`ths_code`, `time`)""".format(table_name=table_name)
                primary_keys_str = "`" + "`, `".join(primary_keys) + "`"
                add_primary_key_str = f",\nADD PRIMARY KEY ({primary_keys_str})"
                chg_pk_str = f"ALTER TABLE {table_name}\n" + "\n".join(col_name_sql_str_list) + add_primary_key_str
                logger.info('对 %s 表创建主键 %s', table_name, primary_keys)
                session.execute(chg_pk_str)

    logger.debug('%s 新增数据 (%d, %d)', table_name, insert_count, df.shape[1])
    return insert_count


def execute_sql(engine, sql_str, commit=False):
    """
    执行给的 sql 语句
    :param engine:
    :param sql_str:
    :param commit:
    :return:
    """
    with with_db_session(engine) as session:
        rslt = session.execute(sql_str)
        insert_count = rslt.rowcount
        if commit:
            session.commit()

    return insert_count


def execute_scalar(engine, sql_str):
    """
    执行查询 sql 语句，返回一个结果值
    :param engine:
    :param sql_str:
    :return:
    """
    with with_db_session(engine) as session:
        return session.scalar(sql_str)


if __name__ == "__main__":
    from sqlalchemy import create_engine

    engine = create_engine("mysql://mg:Dcba1234@localhost/md_integration?charset=utf8",
                           echo=False, encoding="utf-8")
    table_name = 'test_only'
    # if not engine.has_table(table_name):
    #     df = pd.DataFrame({'a': [1.0, 11.0], 'b': [2.0, 22.0], 'c': [3, 33], 'd': [4, 44]})
    #     df.to_sql(table_name, engine, index=False, if_exists='append')
    #     with with_db_session(engine) as session:
    #         session.execute("""ALTER TABLE {table_name}
    #     CHANGE COLUMN a a DOUBLE NOT NULL FIRST,
    #     CHANGE COLUMN d d INTEGER,
    #     ADD PRIMARY KEY (a)""".format(table_name=table_name))

    df = pd.DataFrame({'a': [1.0, 111.0], 'b': [2.2, 222.0], 'c': [np.nan, np.nan]})
    insert_count = bunch_insert_on_duplicate_update(df, table_name, engine, dtype=None, myisam_if_create_table=True,
                                                    primary_keys=['a', 'b'], schema='md_integration')
    print(insert_count)
