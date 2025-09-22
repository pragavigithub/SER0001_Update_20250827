
import os
import logging
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
# from flask_wtf.csrf import CSRFProtect  # Disabled per user request
from sqlalchemy.orm import DeclarativeBase
from werkzeug.middleware.proxy_fix import ProxyFix
from credential_loader import load_credentials_from_json, get_credential

# Configure logging
logging.basicConfig(level=logging.DEBUG)

# Load credentials from JSON file
credentials = load_credentials_from_json()

class Base(DeclarativeBase):
    pass


# Initialize extensions
db = SQLAlchemy(model_class=Base)
login_manager = LoginManager()

# Create Flask app
app = Flask(__name__)
# Configure session secret key (required for Flask-Login)
session_secret = get_credential(credentials,"SESSION_SECRET")
if not session_secret:
    if app.debug or os.environ.get("FLASK_ENV") == "development":
        import os as _os
        session_secret = _os.urandom(32)
        logging.warning("⚠️ SESSION_SECRET not set; using ephemeral development key")
    else:
        raise SystemExit("❌ SESSION_SECRET environment variable is required in production")
app.secret_key = session_secret
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Security configurations for production
app.config['SESSION_COOKIE_SECURE'] = not app.debug
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# -------------------------------
# Database Configuration (MySQL or PostgreSQL)
# -------------------------------

# Try MySQL first (from JSON credentials), then fallback to PostgreSQL
mysql_config = {
    'host': get_credential(credentials, 'MYSQL_HOST', 'localhost'),
    'port': get_credential(credentials, 'MYSQL_PORT', '3306'),
    'user': get_credential(credentials, 'MYSQL_USER', 'root'),
    'password': get_credential(credentials, 'MYSQL_PASSWORD', 'root123'),
    'database': get_credential(credentials, 'MYSQL_DATABASE', 'it_lobby')
}

# Check if we have a custom DATABASE_URL from JSON
database_url_from_json = get_credential(credentials, 'DATABASE_URL')

try:
    if database_url_from_json:
        # Use DATABASE_URL from JSON credentials
        database_url = database_url_from_json
        logging.info(f"Using DATABASE_URL from JSON credentials")
        db_type = "mysql" if "mysql" in database_url else "postgresql"
    elif mysql_config['host'] != 'localhost' or credentials:
        # Use MySQL configuration from JSON
        database_url = (
            f"mysql+pymysql://{mysql_config['user']}:{mysql_config['password']}"
            f"@{mysql_config['host']}:{mysql_config['port']}/{mysql_config['database']}"
        )
        logging.info(f"Using MySQL database from JSON credentials: {mysql_config['host']}")
        db_type = "mysql"
    else:
        # Fallback to Replit's PostgreSQL database
        database_url = os.environ.get('DATABASE_URL')
        if not database_url:
            raise Exception("No database configuration found")
        logging.info("Using PostgreSQL database (connection successful)")
        db_type = "postgresql"

    from sqlalchemy import create_engine, text
    test_engine = create_engine(database_url, connect_args={'connect_timeout': 5})
    with test_engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_recycle": 300,
        "pool_pre_ping": True,
        "pool_size": 10,
        "max_overflow": 20
    }
    logging.info(f"✅ {db_type.upper()} database connection successful")

except Exception as e:
    logging.error(f"❌ Database connection failed: {e}")
    # If MySQL from JSON fails, try PostgreSQL fallback
    from sqlalchemy import create_engine, text
    try:
        database_url = os.environ.get('DATABASE_URL')
        if database_url:
            logging.info("Trying PostgreSQL fallback...")
            test_engine = create_engine(database_url, connect_args={'connect_timeout': 5})
            with test_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            app.config["SQLALCHEMY_DATABASE_URI"] = database_url
            app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
                "pool_recycle": 300,
                "pool_pre_ping": True,
                "pool_size": 10,
                "max_overflow": 20
            }
            db_type = "postgresql"
            logging.info("✅ PostgreSQL fallback connection successful")
        else:
            raise SystemExit("No working database configuration found")
    except Exception as fallback_error:
        logging.error(f"❌ PostgreSQL fallback also failed: {fallback_error}")
        raise SystemExit("Database connection failed. Please check database settings.")

# Store database type
app.config["DB_TYPE"] = db_type

# Initialize extensions
db.init_app(app)
login_manager.init_app(app)
login_manager.login_view = 'login'  # type: ignore
login_manager.login_message = 'Please log in to access this page.'

# CSRF protection disabled per user request
# csrf = CSRFProtect(app)

# CSRF error handler disabled - CSRF protection removed per user request
# @app.errorhandler(400)
# def handle_csrf_error(e):
#     from flask import request, jsonify
#     # Return JSON error for API routes
#     if request.path.startswith('/so-against-invoice/api/') or request.path.startswith('/api/'):
#         return jsonify({
#             'success': False,
#             'error': 'CSRF token validation failed. Please refresh the page and try again.'
#         }), 400
#     # Return default error for non-API routes
#     return e

# SAP B1 Configuration (from JSON credentials)
app.config['SAP_B1_SERVER'] = get_credential(credentials, 'SAP_B1_SERVER', 'https://10.112.253.173:50000')
app.config['SAP_B1_USERNAME'] = get_credential(credentials, 'SAP_B1_USERNAME', 'manager')
app.config['SAP_B1_PASSWORD'] = get_credential(credentials, 'SAP_B1_PASSWORD', '1422')
app.config['SAP_B1_COMPANY_DB'] = get_credential(credentials, 'SAP_B1_COMPANY_DB', 'SBODemoUS')

# Log SAP B1 configuration (without password)
logging.info(f"SAP B1 Server: {app.config['SAP_B1_SERVER']}")
logging.info(f"SAP B1 Username: {app.config['SAP_B1_USERNAME']}")
logging.info(f"SAP B1 Company DB: {app.config['SAP_B1_COMPANY_DB']}")

# Import models
import models
import models_extensions


with app.app_context():
    # Create all tables first to ensure they exist before any queries
    db.create_all()
    logging.info("✅ Database tables created")

    # Drop unique constraint if exists (PostgreSQL version)
    try:
        from sqlalchemy import text
        with db.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT constraint_name 
                FROM information_schema.table_constraints 
                WHERE table_schema = 'public' 
                AND table_name = 'serial_number_transfer_serials' 
                AND constraint_name = 'unique_serial_per_item'
            """))
            if result.fetchone():
                conn.execute(text("ALTER TABLE serial_number_transfer_serials DROP CONSTRAINT unique_serial_per_item"))
                conn.commit()
                logging.info("Dropped unique_serial_per_item constraint")
    except Exception as e:
        logging.warning(f"⚠️ Could not drop unique constraint: {e}")

    # Create default data
    try:
        from models_extensions import Branch
        from werkzeug.security import generate_password_hash
        from models import User

        default_branch = Branch.query.filter_by(id='BR001').first()
        if not default_branch:
            default_branch = Branch()
            default_branch.id = 'BR001'
            default_branch.name = 'Main Branch'
            default_branch.branch_code = 'BR001'
            default_branch.branch_name = 'Main Branch'
            default_branch.description = 'Main Office Branch'
            default_branch.address = 'Main Office'
            default_branch.phone = '123-456-7890'
            default_branch.email = 'main@company.com'
            default_branch.manager_name = 'Branch Manager'
            default_branch.active = True
            default_branch.is_default = True
            db.session.add(default_branch)
            logging.info("✅ Default branch created")

        # Only create admin user in development or if explicitly requested
        if os.environ.get('CREATE_DEFAULT_ADMIN') == 'true' or app.debug:
            admin = User.query.filter_by(username='admin').first()
            if not admin:
                default_password = os.environ.get('ADMIN_PASSWORD', 'admin123')
                admin = User()
                admin.username = 'admin'
                admin.email = 'admin@company.com'
                admin.password_hash = generate_password_hash(default_password)
                admin.first_name = 'System'
                admin.last_name = 'Administrator'
                admin.role = 'admin'
                admin.branch_id = 'BR001'
                admin.branch_name = 'Main Branch'
                admin.default_branch_id = 'BR001'
                admin.active = True
                admin.must_change_password = True  # Force password change
                db.session.add(admin)
                logging.info("✅ Default admin user created (password change required)")

        db.session.commit()
        logging.info("✅ Default data initialized")

    except Exception as e:
        logging.error(f"❌ Error initializing default data: {e}")
        db.session.rollback()

# Setup logging
try:
    from logging_config import setup_logging
    logger = setup_logging(app)
    logger.info("🚀 WMS Application starting with file-based logging")
except Exception as e:
    logging.warning(f"⚠️ Logging setup failed: {e}. Using basic logging.")

# Register all modules through main controller
from modules import main_controller
main_controller.register_modules(app)

# Database tables already created above, this ensures module models are included
with app.app_context():
    db.create_all()  # Safe to call again, will only create missing tables
    logging.info("✅ All module models verified/created")

# Import routes
import routes

