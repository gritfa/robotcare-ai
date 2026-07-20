from sqlalchemy.orm import Session

from .flow_catalog import load_flow_catalog, sync_flow_catalog


def seed_database(db: Session) -> None:
    sync_flow_catalog(db, load_flow_catalog())
