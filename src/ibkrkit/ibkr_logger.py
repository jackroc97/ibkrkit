import json
import os
import sqlite3

from datetime import datetime

from ib_async import *


class IbkrLogger:
    
    def __init__(self, db_file_path: str):
        return
        self.db_file_path = db_file_path
        self.create_database(self.db_file_path)
            
    
    def start_session(self, strategy_name: str, strategy_version: str):
        return
        self.con = sqlite3.connect(self.db_file_path)
        cur = self.con.cursor()
        
        cur.execute(f'''
            INSERT or IGNORE INTO strategy(name, version)
            VALUES ('{strategy_name}', '{strategy_version}')
        ''')
        self.con.commit()
            
        start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S%z")

        cur.execute(f'''
            INSERT INTO session(strategy_name, strategy_version, start_time)
            VALUES('{strategy_name}', '{strategy_version}', '{start_time}')
        ''')
        self.con.commit()
        self.session_id = cur.lastrowid       
    
    
    def end_session(self):
        return
        cur = self.con.cursor()
        end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S%z")
        cur.execute(f'''
            UPDATE session
            SET end_time = '{end_time}'
            WHERE id = {self.session_id}
        ''')
        self.con.commit()
        self.con.close()
        
        
    def log_order(self, order: Order, aux_data: dict = None):
        return
        cur = self.con.cursor()
        time = datetime.now().strftime("%Y-%m-%d %H:%M:%S%z")
        
        cur.execute(f'''
            INSERT or IGNORE INTO [order](id, session_id, parent_id, time, order_type, action, quantity, limit_price, aux_price, oca_group, oca_type, aux_data)
            VALUES({order.orderId}, {self.session_id}, {order.parentId if order.parentId is not None else 'NULL'}, '{time}', '{order.orderType}', '{order.action}', {order.totalQuantity}, {order.lmtPrice if order.lmtPrice is not None else 'NULL'}, {order.auxPrice if order.auxPrice is not None else 'NULL'}, '{order.ocaGroup if order.ocaGroup is not None else 'NULL'}', {order.ocaType if order.ocaType is not None else 'NULL'}, '{json.dumps(aux_data)}')
        ''')
        self.con.commit()
        return cur.lastrowid
    
    
    def log_fill(self, trade: Trade, fill: Fill):
        return
        cur = self.con.cursor()
        time = datetime.now().strftime("%Y-%m-%d %H:%M:%S%z")
        con = fill.contract if fill.contract is not None else trade.contract
        
        cur.execute(f'''
            INSERT or IGNORE INTO fill(exec_id, session_id, time, con_id, con_symbol, con_local_symbol, con_sec_type, con_expiration_date, con_strike, con_option_right, exec_order_id, exec_time, exec_account, exec_exchange, exec_side, exec_shares, exec_price, commission)
            VALUES('{fill.execution.execId}', {self.session_id}, '{time}', {con.conId}, '{con.symbol}', '{con.localSymbol}', '{con.secType}', '{con.lastTradeDateOrContractMonth if hasattr(con, 'lastTradeDateOrContractMonth') else 'NULL'}', {con.strike if hasattr(con, 'strike') else 'NULL'}, '{con.right if hasattr(con, 'right') else 'NULL'}', {fill.execution.orderId}, '{fill.execution.time}', '{fill.execution.acctNumber}', '{fill.execution.exchange}', '{fill.execution.side}', {fill.execution.shares}, {fill.execution.price}, {fill.commissionReport.commission})
        ''')
        self.con.commit()
        return cur.lastrowid
    
    
    @classmethod
    def create_database(cls, db_file_path: str) -> None:
        return
        print("Creating database...")
        
        con = sqlite3.connect(db_file_path)
        cur = con.cursor()
        
        create_strategy_tbl = f'''
            CREATE TABLE strategy(
                name    TEXT NOT NULL,
                version TEXT NOT NULL,
                PRIMARY KEY (name, version)
            )
            '''

        create_session_tbl = f'''
            CREATE TABLE session(
                id                  INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                strategy_name       TEXT NOT NULL,
                strategy_version    TEXT NOT NULL,
                start_time          TEXT NOT NULL,
                end_time            TEXT,
                FOREIGN KEY(strategy_name, strategy_version) REFERENCES strategy(name, version)
            )
            '''
            
        create_order_tbl = f'''
            CREATE TABLE [order](
                id                  INTEGER NOT NULL PRIMARY KEY,
                session_id          INTEGER NOT NULL,
                parent_id           INTEGER,
                time                TEXT NOT NULL,
                order_type          TEXT NOT NULL,
                action              TEXT NOT NULL,
                quantity            REAL NOT NULL,
                limit_price         REAL,
                aux_price           REAL,
                oca_group           TEXT,
                oca_type            INTEGER,
                aux_data            TEXT,    
                FOREIGN KEY(session_id) REFERENCES session(id)
            )
        '''
        
        create_fill_tbl = f'''
            CREATE TABLE fill(
                exec_id             TEXT NOT NULL PRIMARY KEY,
                session_id          INTEGER NOT NULL,
                time                TEXT NOT NULL,
                con_id              INTEGER NOT NULL,
                con_symbol          TEXT NOT NULL,
                con_local_symbol    TEXT NOT NULL,
                con_sec_type        TEXT NOT NULL,
                con_expiration_date TEXT,
                con_strike          REAL,
                con_option_right    TEXT,
                exec_order_id       INTEGER NOT NULL,
                exec_time           TEXT NOT NULL,
                exec_account        TEXT NOT NULL,
                exec_exchange       TEXT NOT NULL,
                exec_side           TEXT NOT NULL,
                exec_shares         REAL NOT NULL,
                exec_price          REAL NOT NULL,
                commission          REAL NOT NULL,
                FOREIGN KEY(session_id) REFERENCES session(id)
                FOREIGN KEY(exec_order_id) REFERENCES [order](id)
            )
        '''
        
        cur.execute("DROP TABLE if EXISTS strategy")
        cur.execute(create_strategy_tbl)
        cur.execute("DROP TABLE if EXISTS session")
        cur.execute(create_session_tbl)
        cur.execute("DROP TABLE if EXISTS [order]")
        cur.execute(create_order_tbl)
        cur.execute("DROP TABLE if EXISTS fill")
        cur.execute(create_fill_tbl)
        
        con.commit()
        con.close()