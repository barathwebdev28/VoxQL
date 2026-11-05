from sqlalchemy import create_engine, MetaData
import os
from dotenv import load_dotenv

load_dotenv()
engine = create_engine(os.getenv("DATABASE_URL"))
metadata = MetaData()
metadata.reflect(bind=engine)

def get_schema_summary():
    summary = {}
    for table_name, table in metadata.tables.items():
        summary[table_name] = [col.name for col in table.columns]
    return summary