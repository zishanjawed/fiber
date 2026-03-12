#!/usr/bin/env python3
"""
Claude Code A/B Testing Submit Script
Validates experiment data and uploads to Supabase for analysis.
"""
import os
import sys
import json
import re
import zipfile
import shutil
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from supabase import create_client, Client
    from tusclient import client as tus_client
except ImportError as e:
    print("❌ ERROR: Required packages not found.")
    print("   Please install them with the following command:")
    print("   pip install supabase tuspy")
    print(f"   (Missing: {e})")
    print("✨ Tip: Copy and paste the install command above into your terminal to continue.✨")
    sys.exit(1)

# Supabase configuration
SUPABASE_URL = "https://sdippjgffrptdvlmlurv.supabase.co"
ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNkaXBwamdmZnJwdGR2bG1sdXJ2Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTk4MTg4MzAsImV4cCI6MjA3NTM5NDgzMH0.f8zJ4fIcZFmzpRpngQ6NWIUudbBptGIO2vb5GBWfc2A"
BUCKET_NAME = "code-preferences-submissions-v2"
SETUP_BUCKET_NAME = "code-preferences-setup-files-v2"

# Initialize Supabase client
# File uploads use Tus resumable protocol which handles large files with chunking
supabase: Client = create_client(SUPABASE_URL, ANON_KEY)

def print_error(message):
    """Print error message and exit."""
    print(f"❌ ERROR: {message}", file=sys.stderr)
    sys.exit(1)

def print_success(message):
    """Print success message."""
    print(f"✅ {message}")

def print_info(message):
    """Print info message."""
    print(f"ℹ️  {message}")

def print_warning(message):
    """Print warning message."""
    print(f"⚠️  WARNING: {message}")

def download_sprint_config():
    """Download sprint configuration from Supabase (in memory only)."""
    try:
        import requests
    except ImportError:
        print_error("'requests' library not found. Install with: pip install requests")
    
    url = f"{SUPABASE_URL}/storage/v1/object/{SETUP_BUCKET_NAME}/config/sprint_config.json"
    headers = {
        "Authorization": f"Bearer {ANON_KEY}",
        "apikey": ANON_KEY
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        
        config = response.json()
        return config
        
    except Exception as e:
        print_error(f"Failed to download sprint configuration: {e}")
        return None

def validate_input(prompt, validator=None, error_msg="Invalid input"):
    """Get and validate user input."""
    while True:
        try:
            value = input(f"{prompt}: ").strip()
            if not value:
                print(f"❌ {error_msg}: Input cannot be empty")
                continue
            if validator and not validator(value):
                print(f"❌ {error_msg}")
                continue
            return value
        except KeyboardInterrupt:
            print("\n❌ Submission cancelled by user")
            sys.exit(1)

def validate_folder_name(name):
    """Validate user folder name format."""
    if not name:
        return False
    # Check for valid characters (alphanumeric, underscore, hyphen)
    return bool(re.match(r'^[a-zA-Z0-9_-]+$', name)) and len(name) >= 3

def read_manifest():
    """Read and validate manifest.json file."""
    manifest_path = Path("manifest.json")
    
    if not manifest_path.exists():
        print_error("manifest.json not found. Make sure you're in the experiment directory.")
    
    try:
        with open(manifest_path, 'r') as f:
            manifest = json.load(f)
        
        required_fields = ["expert_name", "task_id", "timestamp", "repo_url", "assignments"]
        missing_fields = [field for field in required_fields if field not in manifest]
        
        if missing_fields:
            print_error(f"Invalid manifest.json. Missing fields: {', '.join(missing_fields)}")
        
        return manifest
        
    except json.JSONDecodeError as e:
        print_error(f"Invalid JSON in manifest.json: {e}")
    except Exception as e:
        print_error(f"Failed to read manifest.json: {e}")

def extract_session_id(filename):
    """Extract session ID from filename like 'session_abc123.jsonl' or 'session_abc123_raw.jsonl'."""
    match = re.search(r'session_([^.]+)', filename)
    if match:
        session_id = match.group(1)
        # Remove _raw suffix if present (it's a valid variation of the same session)
        session_id = session_id.replace('_raw', '')
        return session_id
    return None

def check_session_summary_exists(session_file_path):
    """Check if session_summary event exists in a session log file."""
    try:
        with open(session_file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    # Check for both old 'event_type' and new 'type' field for compatibility
                    if event.get('type') == 'session_summary' or event.get('event_type') == 'session_summary':
                        return True
                except json.JSONDecodeError:
                    continue
        return False
    except Exception as e:
        print_warning(f"Failed to read session file {session_file_path}: {e}")
        return False

def validate_experiment_files():
    """Validate that all required experiment files exist."""
    # Check manifest.json exists
    if not Path("manifest.json").is_file():
        print_error("manifest.json not found in current directory")
    
    # Check snapshots folder exists
    if not Path("snapshots").is_dir():
        print_error("snapshots folder not found in current directory")
    
    required_files = [
        "manifest.json",
        "model_a/.claude/settings.local.json",
        "model_b/.claude/settings.local.json"
    ]
    
    required_dirs = [
        "model_a",
        "model_b",
        "logs",
        "snapshots"
    ]
    
    # Check required files
    missing_files = []
    for file_path in required_files:
        if not Path(file_path).is_file():
            missing_files.append(file_path)
    
    # Check required directories
    missing_dirs = []
    for dir_path in required_dirs:
        if not Path(dir_path).is_dir():
            missing_dirs.append(dir_path)
    
    # Check for session logs with simplified validation
    # Required: mandatory session file (session_*.jsonl, not _raw.jsonl)
    # Optional: raw session file (session_*_raw.jsonl) - warning if missing
    logs_dir = Path("logs")
    if logs_dir.exists():
        # Validate model_a logs
        model_a_dir = logs_dir / "model_a"
        model_a_all_logs = list(model_a_dir.glob("session_*.jsonl"))
        
        # Separate mandatory and raw files
        model_a_mandatory = [f for f in model_a_all_logs if not f.name.endswith("_raw.jsonl")]
        model_a_raw = [f for f in model_a_all_logs if f.name.endswith("_raw.jsonl")]
        
        # Require exactly 1 mandatory session file
        if not model_a_mandatory:
            print_error("No mandatory session log file found in logs/model_a/ (expected session_*.jsonl)")
        elif len(model_a_mandatory) > 1:
            print_error(f"Found {len(model_a_mandatory)} mandatory session files in logs/model_a/, expected exactly 1")
        else:
            mandatory_file = model_a_mandatory[0]
            session_id_a = extract_session_id(mandatory_file.name)
            print_success(f"Model A: Found mandatory session file '{mandatory_file.name}' with session ID '{session_id_a}'")
            
            # Check for session_summary in the mandatory file
            if not check_session_summary_exists(mandatory_file):
                print_warning(f"Model A: session_summary event not found in '{mandatory_file.name}' - API extraction may fail")
            else:
                print_success(f"Model A: session_summary event found in '{mandatory_file.name}'")
            
            # Check for corresponding raw file
            if not model_a_raw:
                print_warning(f"Model A: Raw session file (session_{session_id_a}_raw.jsonl) not found - continuing anyway")
            else:
                raw_file = model_a_raw[0]
                raw_session_id = extract_session_id(raw_file.name)
                if raw_session_id == session_id_a:
                    print_success(f"Model A: Found raw session file '{raw_file.name}'")
                else:
                    print_warning(f"Model A: Raw session file has different session ID (expected '{session_id_a}', found '{raw_session_id}')")
        
        # Validate model_b logs
        model_b_dir = logs_dir / "model_b"
        model_b_all_logs = list(model_b_dir.glob("session_*.jsonl"))
        
        # Separate mandatory and raw files
        model_b_mandatory = [f for f in model_b_all_logs if not f.name.endswith("_raw.jsonl")]
        model_b_raw = [f for f in model_b_all_logs if f.name.endswith("_raw.jsonl")]
        
        # Require exactly 1 mandatory session file
        if not model_b_mandatory:
            print_error("No mandatory session log file found in logs/model_b/ (expected session_*.jsonl)")
        elif len(model_b_mandatory) > 1:
            print_error(f"Found {len(model_b_mandatory)} mandatory session files in logs/model_b/, expected exactly 1")
        else:
            mandatory_file = model_b_mandatory[0]
            session_id_b = extract_session_id(mandatory_file.name)
            print_success(f"Model B: Found mandatory session file '{mandatory_file.name}' with session ID '{session_id_b}'")
            
            # Check for session_summary in the mandatory file
            if not check_session_summary_exists(mandatory_file):
                print_warning(f"Model B: session_summary event not found in '{mandatory_file.name}' - API extraction may fail")
            else:
                print_success(f"Model B: session_summary event found in '{mandatory_file.name}'")
            
            # Check for corresponding raw file
            if not model_b_raw:
                print_warning(f"Model B: Raw session file (session_{session_id_b}_raw.jsonl) not found - continuing anyway")
            else:
                raw_file = model_b_raw[0]
                raw_session_id = extract_session_id(raw_file.name)
                if raw_session_id == session_id_b:
                    print_success(f"Model B: Found raw session file '{raw_file.name}'")
                else:
                    print_warning(f"Model B: Raw session file has different session ID (expected '{session_id_b}', found '{raw_session_id}')")
    
    # Check for snapshots
    snapshots_dir = Path("snapshots")
    if snapshots_dir.exists():
        expected_snapshots = [
            "model_a_start.zip",
            "model_a_end.zip", 
            "model_a_diff.patch",
            "model_b_start.zip",
            "model_b_end.zip",
            "model_b_diff.patch"
        ]
        
        missing_snapshots = []
        for snapshot in expected_snapshots:
            snapshot_path = snapshots_dir / snapshot
            if not snapshot_path.exists():
                missing_snapshots.append(f"snapshots/{snapshot}")
        
        if missing_snapshots:
            print_warning(f"Some snapshots are missing: {', '.join(missing_snapshots)}")
            print_warning("This might indicate incomplete sessions. Continuing anyway...")
    
    # Report missing items
    all_missing = missing_files + missing_dirs
    if all_missing:
        print_error(f"Validation failed. Missing required items:\n" + 
                   "\n".join(f"  - {item}" for item in all_missing))
    
    print_success("Experiment files validation passed")
    return True

def create_snapshots_zip():
    """Create a zip file of the entire snapshots directory."""
    snapshots_dir = Path("snapshots")
    if not snapshots_dir.exists():
        return None
    
    zip_filename = "snapshots.zip"
    print_info(f"Creating snapshots archive: {zip_filename}")
    
    try:
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_path in snapshots_dir.rglob("*"):
                if file_path.is_file():
                    # Add file to zip with relative path from current directory
                    arcname = str(file_path).replace('\\', '/')
                    zipf.write(file_path, arcname=arcname)
                    print_info(f"  Added to archive: {arcname}")
        
        print_success(f"Created snapshots archive: {zip_filename}")
        return zip_filename
    except Exception as e:
        print_warning(f"Failed to create snapshots archive: {e}")
        return None

def get_file_list_for_upload():
    """Get list of all files to upload."""
    upload_files = []
    
    # Always include manifest
    upload_files.append("manifest.json")
    
    # Include all logs
    logs_dir = Path("logs")
    if logs_dir.exists():
        for log_file in logs_dir.rglob("*.jsonl"):
            # Convert to forward slashes for consistent paths
            upload_files.append(str(log_file).replace('\\', '/'))
    
    # Create and include snapshots zip file (instead of individual files)
    snapshots_zip = create_snapshots_zip()
    if snapshots_zip:
        upload_files.append(snapshots_zip)
    
    # Include model configurations (but not the full repos)
    for model in ["model_a", "model_b"]:
        claude_dir = Path(model) / ".claude"
        if claude_dir.exists():
            for config_file in claude_dir.rglob("*"):
                if config_file.is_file():
                    # Skip cache files, bytecode, and system files
                    file_path_str = str(config_file)
                    file_name = config_file.name
                    
                    # Skip patterns
                    if '__pycache__' in file_path_str:
                        continue
                    if file_name.endswith(('.pyc', '.pyo', '.DS_Store')):
                        continue
                    if file_name in {'.DS_Store', 'Thumbs.db', 'desktop.ini'}:
                        continue
                    
                    # Convert to forward slashes for consistent paths
                    upload_files.append(file_path_str.replace('\\', '/'))
    
    return upload_files

def folder_exists_in_bucket(folder_path, bucket_name=None):
    """Check if folder exists in Supabase bucket by trying to list its contents."""
    if bucket_name is None:
        bucket_name = BUCKET_NAME
    
    try:
        # Try to list files in the folder
        result = supabase.storage.from_(bucket_name).list(folder_path)
        # Folder exists only if it contains at least one file (object storage has no empty folders)
        return len(result) > 0
    except Exception as e:
        error_msg = str(e)
        # 404 or "not found" means folder doesn't exist
        if "404" in error_msg or "not found" in error_msg.lower():
            return False
        # For other errors, assume folder might exist (safe approach)
        return True

def get_available_upload_path(sprint_folder, task_id):
    """Find an available folder path with versioning within sprint folder."""
    print_info("Checking for existing submissions...")
    
    # Try base path first: sprint_folder/task_id
    base_path = f"{sprint_folder}/{task_id}"
    if not folder_exists_in_bucket(base_path):
        print_success(f"Using upload path: {base_path}/")
        return base_path
    
    print_info(f"Found existing submission at {base_path}/")
    
    # Try versioned paths: sprint_folder/task_id_v1, task_id_v2, etc.
    version = 1
    while version <= 100:  # Safety limit
        versioned_path = f"{sprint_folder}/{task_id}_v{version}"
        if not folder_exists_in_bucket(versioned_path):
            print_success(f"Using upload path: {versioned_path}/")
            return versioned_path
        
        print_info(f"Found existing submission at {versioned_path}/")
        version += 1
    
    # If we hit the limit, raise an error
    print_error(f"Too many versions for task {task_id}. Maximum 100 versions reached.")

def upload_file_to_supabase(local_file_path, remote_file_path):
    """Upload a single file to Supabase storage using Tus resumable upload (supports large files)."""
    try:
        file_path = Path(local_file_path)
        file_size = file_path.stat().st_size
        file_size_mb = file_size / (1024 * 1024)
        
        print_info(f"  File size: {file_size_mb:.2f} MB")
        
        # Use Tus resumable upload for reliable large file uploads
        # Create Tus client for resumable uploads
        my_client = tus_client.TusClient(
            f"{SUPABASE_URL}/storage/v1/upload/resumable",
            headers={
                "Authorization": f"Bearer {ANON_KEY}",
                "x-upsert": "true"  # Overwrite if file exists
            }
        )
        
        # Open file and upload with chunking
        with open(local_file_path, 'rb') as file_stream:
            uploader = my_client.uploader(
                file_stream=file_stream,
                chunk_size=(50 * 1024 * 1024),  # 50MB chunks
                metadata={
                    "bucketName": BUCKET_NAME,
                    "objectName": remote_file_path,
                    "contentType": "application/octet-stream",
                    "cacheControl": "3600"
                }
            )
            uploader.upload()
        
        return True
            
    except Exception as e:
        error_msg = str(e)
        # Check if file already exists (though x-upsert should handle this)
        if "duplicate" in error_msg.lower() or "already exists" in error_msg.lower():
            print_warning(f"File already exists, skipping: {local_file_path}")
            return True
        else:
            print_error(f"Failed to upload {local_file_path}: {e}")
            return False

def upload_experiment_data(task_id, user_folder, upload_files, manifest):
    """Upload all experiment files to Supabase with simple parallelization (3 workers)."""
    print_info(f"Uploading {len(upload_files)} files...")
    
    uploaded_count = 0
    failed_files = []
    
    # Use ThreadPoolExecutor with 3 workers for parallel uploads
    with ThreadPoolExecutor(max_workers=5) as executor:
        # Submit all upload tasks
        future_to_file = {}
        for local_file in upload_files:
            # Create remote path: TASK_ID/local_file_path (normalize path separators for cross-platform)
            normalized_file_path = local_file.replace('\\', '/')  # Convert Windows backslashes to forward slashes
            remote_file_path = f"{task_id}/{normalized_file_path}"
            
            future = executor.submit(upload_file_to_supabase, local_file, remote_file_path)
            future_to_file[future] = local_file
        
        # Process completed uploads as they finish
        for future in as_completed(future_to_file):
            local_file = future_to_file[future]
            print_info(f"Uploading {local_file}...")
            
            try:
                if future.result():
                    uploaded_count += 1
                    print_success(f"Uploaded {local_file}")
                else:
                    failed_files.append(local_file)
            except Exception as e:
                print_warning(f"Exception uploading {local_file}: {e}")
                failed_files.append(local_file)
    
    if failed_files:
        print_error(f"Upload failed for {len(failed_files)} files:\n" + 
                   "\n".join(f"  - {file}" for file in failed_files))
    
    print_success(f"Successfully uploaded {uploaded_count}/{len(upload_files)} files")
    return len(failed_files) == 0

def create_submission_summary(manifest, user_folder, upload_files):
    """Create a submission summary file."""
    summary = {
        "submission_timestamp": datetime.now(timezone.utc).isoformat(),
        "user_folder_name": user_folder,
        "expert_name": manifest["expert_name"],
        "task_id": manifest["task_id"],
        "experiment_timestamp": manifest["timestamp"],
        "uploaded_files_count": len(upload_files),
        "uploaded_files": upload_files,
        "submission_metadata": {
            "submit_script_version": "1.0.0",
            "upload_path": f"{user_folder}/"
        }
    }
    
    try:
        with open("submission_summary.json", 'w') as f:
            json.dump(summary, f, indent=2)
        
        # Upload the summary as well (normalize path for cross-platform)
        remote_path = f"{user_folder}/submission_summary.json"
        if upload_file_to_supabase("submission_summary.json", remote_path):
            print_success("Created and uploaded submission summary")
            return True
        else:
            print_warning("Created submission summary but failed to upload it")
            return False
            
    except Exception as e:
        print_warning(f"Failed to create submission summary: {e}")
        return False

def cleanup_temp_files():
    """Remove temporary files created during submission."""
    temp_files = ["snapshots.zip", "submission_summary.json"]
    
    for temp_file in temp_files:
        if Path(temp_file).exists():
            try:
                Path(temp_file).unlink()
                print_info(f"Cleaned up temporary file: {temp_file}")
            except Exception as e:
                print_warning(f"Failed to clean up {temp_file}: {e}")

def main():
    """Main submission function."""
    print("📤 Claude Code A/B Testing Submission")
    print("=" * 50)
    
    try:
        # Download sprint configuration
        print_info("Loading sprint configuration...")
        sprint_config = download_sprint_config()
        if not sprint_config:
            print_error("Failed to load sprint configuration")
        
        sprint_folder = sprint_config.get("submission", {}).get("sprint_folder")
        if not sprint_folder:
            print_error("Invalid sprint configuration: missing sprint_folder")
        
        # Read manifest
        print_info("Reading experiment manifest...")
        manifest = read_manifest()
        
        expert_name = manifest["expert_name"]
        task_id = manifest["task_id"]
        
        print_info(f"Expert: {expert_name}")
        print_info(f"Task ID: {task_id}")
        
        # Get available upload path (with versioning if needed)
        upload_path = get_available_upload_path(sprint_folder, task_id)
        
        # Use upload_path as the folder name
        user_folder = upload_path
        
        # Validate experiment files
        print_info("Validating experiment files...")
        validate_experiment_files()
        
        # Get list of files to upload
        print_info("Preparing file list for upload...")
        upload_files = get_file_list_for_upload()
        
        if not upload_files:
            print_error("No files found to upload")
        
        print_info(f"Found {len(upload_files)} files to upload")
        
        # Confirm upload
        print(f"\n📋 Upload Summary:")
        print(f"  Expert: {expert_name}")
        print(f"  Task ID: {task_id}")
        print(f"  Files to upload: {len(upload_files)}")
        print(f"  Upload path: {upload_path}/")
        
        confirm = input(f"\nProceed with upload? (y/N): ").strip().lower()
        if confirm not in ['y', 'yes']:
            print("❌ Upload cancelled by user")
            sys.exit(0)
        
        # Upload files
        print_info("Starting upload...")
        if not upload_experiment_data(upload_path, user_folder, upload_files, manifest):
            # Clean up temporary files before exiting
            cleanup_temp_files()
            print_error("Upload failed")
        
        # Create submission summary
        create_submission_summary(manifest, user_folder, upload_files)
        
        # Clean up temporary files after successful upload
        cleanup_temp_files()
        
        # Success message
        print("\n" + "=" * 50)
        print_success("Submission completed successfully!")
        print(f"\n📊 Your experiment data has been uploaded to:")
        print(f"   Path: {upload_path}/")
        print(f"   Files: {len(upload_files)} files uploaded")
        print("\n🎉 Thank you for your contribution!")
        
    except KeyboardInterrupt:
        print("\n❌ Submission cancelled by user")
        cleanup_temp_files()
        sys.exit(1)
    except Exception as e:
        cleanup_temp_files()
        print_error(f"Unexpected error during submission: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
