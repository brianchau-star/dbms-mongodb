"""Shared connection helper."""
import os
from pathlib import Path

from pymongo import MongoClient

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

MONGO_URI = os.getenv(
    "MONGO_URI", "mongodb://admin:admin123@localhost:27018/?directConnection=true"
)
DB_NAME = os.getenv("MONGO_DB", "spine_mri")


def get_client() -> MongoClient:
    return MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)


def get_db():
    return get_client()[DB_NAME]
