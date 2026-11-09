import os
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure
import ssl
import logging
from dotenv import load_dotenv

load_dotenv()

# MongoDB Configuration
MONGODB_URI = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
if not MONGODB_URI:
    logging.error("❌ MONGODB_URI is not set in environment variables!")

def resolve_database_name(uri: str = None) -> str:
    """
    Resolve the database name with proper separation from other bots.
    Priority:
      1. DATABASE_NAME / MONGO_DB_NAME / MONGO_DATABASE environment variables
      2. Database path in the MongoDB URI (e.g. mongodb+srv://.../Doraemon?...)
      3. Default fallback: 'Doraemon' (ensuring full isolation from Titanic Saver's 'GROUPHELP')
    """
    for key in ("DATABASE_NAME", "MONGO_DB_NAME", "MONGO_DATABASE"):
        val = os.getenv(key)
        if val and val.strip():
            return val.strip()

    cand_uri = uri or os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or MONGODB_URI
    if cand_uri:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(cand_uri)
            db_from_path = parsed.path.lstrip("/").split("?")[0].strip()
            if db_from_path:
                return db_from_path
        except Exception:
            pass

    return "Doraemon"

DATABASE_NAME = resolve_database_name()
COLLECTION_PREFIX = os.getenv("COLLECTION_PREFIX", "").strip()

# Global database client and database instances
client = None
db = None

def connect_to_mongodb():
    """
    Establish connection to MongoDB cluster.
    Returns True if successful, False otherwise.
    """
    global client, db
    
    uri = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or MONGODB_URI
    if not uri:
        logging.error("❌ MONGODB_URI is not set!")
        return False
        
    db_name = resolve_database_name(uri)
    try:
        if client is None:
            # Try multiple connection strategies
            import certifi
            
            # Strategy 1: Use certifi CA file
            try:
                client = MongoClient(
                    uri,
                    serverSelectionTimeoutMS=10000,
                    tls=True,
                    tlsCAFile=certifi.where(),
                    connectTimeoutMS=10000,
                    socketTimeoutMS=10000,
                    maxPoolSize=50,
                    minPoolSize=10,
                    maxIdleTimeMS=60000
                )
                client.admin.command('ping')
                db = client[db_name]
                logging.info(f"✅ Successfully connected to MongoDB database: {db_name}")
                return True
            except Exception as e1:
                logging.warning(f"First connection strategy failed: {e1}")
                
                # Strategy 2: Use system CA certificates
                try:
                    client = MongoClient(
                        uri,
                        serverSelectionTimeoutMS=10000,
                        tls=True,
                        tlsCAFile='/etc/ssl/certs/ca-certificates.crt',
                        connectTimeoutMS=10000,
                        socketTimeoutMS=10000
                    )
                    client.admin.command('ping')
                    db = client[db_name]
                    logging.info(f"✅ Successfully connected to MongoDB (system CA): {db_name}")
                    return True
                except Exception as e2:
                    logging.warning(f"Second connection strategy failed: {e2}")
                    
                    # Strategy 3: Disable certificate validation (NOT recommended for production)
                    logging.warning("Trying connection with TLS disabled as fallback...")
                    client = MongoClient(
                        uri,
                        serverSelectionTimeoutMS=10000,
                        tls=False,
                        connectTimeoutMS=10000,
                        socketTimeoutMS=10000
                    )
                    client.admin.command('ping')
                    db = client[db_name]
                    logging.warning(f"⚠️  Connected to MongoDB with TLS disabled (insecure!): {db_name}")
                    return True
                
        return True
    except ConnectionFailure as e:
        logging.error(f"❌ Failed to connect to MongoDB: {e}")
        return False
    except OperationFailure as e:
        logging.error(f"❌ MongoDB authentication failed: {e}")
        return False
    except Exception as e:
        logging.error(f"❌ Unexpected MongoDB error: {e}")
        return False

def get_database():
    """Get the database instance."""
    global db
    if db is None:
        if not connect_to_mongodb():
            return None
    return db

def get_collection(collection_name):
    """Get a collection from the database."""
    database = get_database()
    if database is None:
        return None
    actual_name = f"{COLLECTION_PREFIX}{collection_name}" if COLLECTION_PREFIX else collection_name
    return database[actual_name]

def is_connected():
    """Check if MongoDB is connected."""
    global db
    return db is not None

def close_connection():
    """Close the MongoDB connection."""
    global client
    if client:
        client.close()
        logging.info("MongoDB connection closed.")

# Collection Names
COLLECTIONS = {
    "users": "users",
    "settings": "chat_settings",
    "SETTINGS": "chat_settings",
    "warns": "warns",
    "muters": "muters",
    "voice_chat_managers": "voice_chat_managers",
    "banned_channels": "banned_channels",
    "blocked_content": "blocked_content",
    "filters": "filters",
    "group_muters": "group_muters",
    "admins": "admins",
    "hidden_messages": "hidden_messages",
    "rankings": "chat_rankings",
    "ranking_meta": "ranking_meta"
}

def get_all_chats():
    """Get all unique chat IDs from the settings collection."""
    settings_col = get_collection(COLLECTIONS["settings"])
    if settings_col is None:
        logging.error("Cannot get chats - database not connected")
        return []
    
    try:
        chats = settings_col.find({}, {"chat_id": 1})
        return list(chats)
    except Exception as e:
        logging.error(f"Error getting all chats: {e}")
        return []

def initialize_collections():
    """Initialize all collections with proper indexes."""
    database = get_database()
    if database is None:
        logging.error("Cannot initialize collections - database not connected")
        return False
    
    try:
        # Users collection indexes
        users_col = get_collection(COLLECTIONS["users"])
        users_col.create_index("id", unique=True)
        users_col.create_index("username")
        
        # Settings collection indexes
        settings_col = get_collection(COLLECTIONS["settings"])
        settings_col.create_index("chat_id", unique=True)
        
        # Warns collection indexes
        warns_col = get_collection(COLLECTIONS["warns"])
        warns_col.create_index([("chat_id", 1), ("user_id", 1)], unique=True)
        
        # Muters collection indexes
        muters_col = get_collection(COLLECTIONS["muters"])
        muters_col.create_index([("chat_id", 1), ("user_id", 1)], unique=True)
        
        # Voice chat managers indexes
        vcm_col = get_collection(COLLECTIONS["voice_chat_managers"])
        vcm_col.create_index([("chat_id", 1), ("user_id", 1)], unique=True)
        
        # Banned channels indexes
        banned_col = get_collection(COLLECTIONS["banned_channels"])
        banned_col.create_index([("chat_id", 1), ("channel_id", 1)], unique=True)
        
        # Blocked content indexes
        blocked_col = get_collection(COLLECTIONS["blocked_content"])
        blocked_col.create_index("chat_id")

        # Hidden messages indexes
        hidden_col = get_collection(COLLECTIONS["hidden_messages"])
        hidden_col.create_index("msg_id", unique=True)
        hidden_col.create_index("expires_at", expireAfterSeconds=0) # Auto cleanup
        
        # Filters indexes
        filters_col = get_collection(COLLECTIONS["filters"])
        filters_col.create_index([("chat_id", 1), ("trigger", 1)], unique=True)

        # Admins collection indexes
        admins_col = get_collection(COLLECTIONS["admins"])
        admins_col.create_index([("chat_id", 1), ("user_id", 1)], unique=True)

        # Rankings collection indexes
        rankings_col = get_collection(COLLECTIONS["rankings"])
        if rankings_col is not None:
            rankings_col.create_index([("chat_id", 1), ("user_id", 1)], unique=True)
            rankings_col.create_index([("chat_id", 1), ("total_messages", -1)])

        # Ranking metadata indexes
        meta_col = get_collection(COLLECTIONS["ranking_meta"])
        if meta_col is not None:
            meta_col.create_index("chat_id", unique=True)
        
        logging.info("✅ MongoDB collections and indexes initialized successfully")
        return True
    except Exception as e:
        logging.error(f"❌ Error initializing collections: {e}")
        return False
