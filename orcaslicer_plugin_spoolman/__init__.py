# /// script
# dependencies = ["requests"]
# [tool.orcaslicer.plugin]
# name = "Spoolman Bridge"
# description = "Integrates OrcaSlicer with self-hosted Spoolman."
# author = "Zao Soula"
# version = "1.0.0"
# ///
import orca
import orca.slicing
import requests
import json
import os
import re
import threading
import time

# Load/save settings in the same directory as the plugin
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(PLUGIN_DIR, "spoolman_settings.json")
LOG_FILE = os.path.join(PLUGIN_DIR, "spoolman_gcode.log")

# Resolved app support directories
SUPPORT_DIR = os.path.dirname(os.path.dirname(PLUGIN_DIR)) # App Support/OrcaSlicer
SYSTEM_DIR = os.path.join(SUPPORT_DIR, "system")
USER_DIR = os.path.join(SUPPORT_DIR, "user")

def get_settings():
    defaults = {"spoolman_url": "http://localhost:7912"}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return {**defaults, **json.load(f)}
        except Exception:
            pass
    return defaults

def write_log(line):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def get_user_subdirs():
    if not os.path.exists(USER_DIR):
        return []
    subdirs = []
    try:
        for name in os.listdir(USER_DIR):
            path = os.path.join(USER_DIR, name)
            if os.path.isdir(path):
                subdirs.append(path)
    except Exception:
        pass
    return subdirs

def inject_plugin_metadata(preset_data):
    ref_str = "spoolman_bridge;;Filament Usage Updater"
    cap_name = "Filament Usage Updater"
    modified = False
    
    plugins = preset_data.get("plugins", [])
    if not isinstance(plugins, list):
        plugins = [plugins] if plugins else []
    if ref_str not in plugins:
        plugins.append(ref_str)
        preset_data["plugins"] = plugins
        modified = True
        
    pipeline = preset_data.get("slicing_pipeline_plugin", [])
    if not isinstance(pipeline, list):
        pipeline = [pipeline] if pipeline else []
    if cap_name not in pipeline:
        pipeline.append(cap_name)
        preset_data["slicing_pipeline_plugin"] = pipeline
        modified = True
        
    return modified

def find_best_spool(spools, gcode_vendor, gcode_material, gcode_color, gcode_preset_name):
    best_spool = None
    best_score = -1
    
    # Normalize G-code variables
    g_vendor = (gcode_vendor or "").strip().lower()
    g_material = (gcode_material or "").strip().lower()
    g_color = (gcode_color or "").strip().lower().replace("#", "")
    g_preset = (gcode_preset_name or "").strip().lower()

    for spool in spools:
        filament = spool.get("filament") or {}
        spool_id = str(spool.get("id"))
        
        # Extract Spoolman properties
        s_vendor = (filament.get("vendor", {}) or {}).get("name", "").strip().lower()
        s_material = (filament.get("material") or "").strip().lower()
        s_color = (filament.get("color_hex") or "").strip().lower()
        s_name = (filament.get("name") or "").strip().lower()
        
        score = 0
        
        # 1. Material is mandatory. If material is defined and doesn't match, skip.
        if g_material and s_material:
            if g_material == s_material:
                score += 10
            else:
                continue
        
        # 2. Vendor match
        if g_vendor and s_vendor:
            if g_vendor == s_vendor or g_vendor in s_vendor or s_vendor in g_vendor:
                score += 5
                
        # 3. Color match
        if g_color and s_color:
            if g_color == s_color:
                score += 8
                
        # 4. Spool ID mentioned in the preset name (e.g., "eSUN PLA White #2")
        if f"#{spool_id}" in g_preset or f"spool {spool_id}" in g_preset:
            score += 20
            
        # 5. Filament Name match in preset name
        if s_name and s_name in g_preset:
            score += 5

        if score > best_score:
            best_score = score
            best_spool = spool
            
    return best_spool, best_score

def sanitize_filename(name):
    # Remove forbidden characters for filesystem safety: \ / : * ? " < > |
    return re.sub(r'[\\/*?:"<>|]', "", name)

def sync_spoolman_filaments(dry_run=False):
    synced_count = 0
    modified_any = False
    try:
        settings = get_settings()
        url = settings["spoolman_url"].rstrip('/')
        r = requests.get(f"{url}/api/v1/spool?allow_archived=false", timeout=5)
        if r.status_code != 200:
            return 0, False
        spools = r.json()
    except Exception:
        return 0, False

    try:
        if not os.path.exists(USER_DIR):
            return 0, False

        # Gather all system filament preset names ending with @System
        system_filaments = []
        if os.path.exists(SYSTEM_DIR):
            for root, dirs, files in os.walk(SYSTEM_DIR):
                if "filament" in root:
                    for file in files:
                        if file.endswith(".json"):
                            try:
                                with open(os.path.join(root, file), "r", encoding="utf-8") as f:
                                    fdata = json.load(f)
                                name = fdata.get("name")
                                if name and name.endswith("@System"):
                                    system_filaments.append(name)
                            except Exception:
                                pass

        user_subdirs = get_user_subdirs()

        active_ids = set()
        for spool in spools:
            spool_id = spool.get("id")
            if not spool_id:
                continue
            active_ids.add(str(spool_id))
            
            filament = spool.get("filament") or {}
            vendor = filament.get("vendor") or {}
            vendor_name = vendor.get("name") or "Generic"
            filament_name = filament.get("name") or "PLA"
            color_hex = filament.get("color_hex") or "FFFFFF"
            if not color_hex.startswith("#"):
                color_hex = "#" + color_hex
            material = (filament.get("material") or "PLA").upper()
            
            # Map material to base system preset
            base_material = "PLA"
            for m in ["PLA", "PETG", "ABS", "TPU", "ASA"]:
                if m in material:
                    base_material = m
                    break
            
            extruder_temp = filament.get("extruder_temp")
            bed_temp = filament.get("bed_temp")
            
            # Get spool/filament price and weight to calculate cost per kg (OrcaSlicer expects price per kg)
            price = spool.get("price") or filament.get("price")
            weight_g = filament.get("weight") or 1000.0
            
            # Map Spoolman price to OrcaSlicer filament_cost (cost per kg)
            calculated_cost = None
            if price is not None and float(weight_g) > 0:
                calculated_cost = (float(price) / float(weight_g)) * 1000.0
            
            preset_name = f"{vendor_name} {filament_name} (#{spool_id}) - Spoolman"
            
            for subdir in user_subdirs:
                fil_dir = os.path.join(subdir, "filament")
                os.makedirs(fil_dir, exist_ok=True)
                
                filename = sanitize_filename(preset_name) + ".json"
                filepath = os.path.join(fil_dir, filename)
                
                # Load existing data to avoid wiping out user UI edits
                preset_data = {}
                if os.path.exists(filepath):
                    try:
                        with open(filepath, "r", encoding="utf-8") as existing_f:
                            preset_data = json.load(existing_f)
                    except Exception:
                        pass
                
                # If new/empty, establish base inheritance
                if not preset_data:
                    best_system_match = None
                    for sys_name in system_filaments:
                        if vendor_name.lower() in sys_name.lower() and base_material.lower() in sys_name.lower():
                            best_system_match = sys_name
                            break
                    preset_data["inherits"] = best_system_match if best_system_match else f"Generic {base_material} @System"
                
                # Overlay Spoolman managed fields
                preset_data["default_filament_colour"] = [color_hex]
                preset_data["filament_settings_id"] = [preset_name]
                preset_data["from"] = "User"
                preset_data["name"] = preset_name
                preset_data["filament_vendor"] = ["Spoolman"]
                
                # Ensure plugin hooks are injected
                inject_plugin_metadata(preset_data)
                
                preset_data["version"] = "2.4.0.3"

                # Use built-in temperatures if set in Spoolman
                if extruder_temp and int(extruder_temp) > 0:
                    t = int(extruder_temp)
                    preset_data["nozzle_temperature"] = [str(t)]
                    preset_data["nozzle_temperature_initial_layer"] = [str(t)]
                    preset_data["nozzle_temperature_range_low"] = [str(t - 15)]
                    preset_data["nozzle_temperature_range_high"] = [str(t + 15)]
                    
                if bed_temp and int(bed_temp) > 0:
                    b = int(bed_temp)
                    preset_data["bed_temperature"] = [str(b)]
                    preset_data["bed_temperature_initial_layer"] = [str(b)]

                # Use calculated filament cost per kg
                if calculated_cost is not None:
                    preset_data["filament_cost"] = [f"{calculated_cost:.2f}"]

                # Read Spoolman extra fields if present
                extra = {**(filament.get("extra") or {}), **(spool.get("extra") or {})}
                if "max_volumetric_speed" in extra and extra["max_volumetric_speed"] is not None:
                    preset_data["filament_max_volumetric_speed"] = [str(extra["max_volumetric_speed"])]
                
                # Compare with file on disk to see if we need to write changes
                needs_write = True
                if os.path.exists(filepath):
                    try:
                        with open(filepath, "r", encoding="utf-8") as check_f:
                            if json.load(check_f) == preset_data:
                                needs_write = False
                    except Exception:
                        pass
                
                if needs_write:
                    if not dry_run:
                        with open(filepath, "w", encoding="utf-8") as f:
                            json.dump(preset_data, f, indent=4)
                    modified_any = True
            synced_count += 1
                    
        # Clean up archived/deleted spool presets
        for subdir in user_subdirs:
            fil_dir = os.path.join(subdir, "filament")
            if os.path.exists(fil_dir):
                for filename in os.listdir(fil_dir):
                    if filename.endswith(" - Spoolman.json"):
                        parts = re.search(r"\(#(\d+)\)", filename)
                        if parts:
                            spool_id_str = parts.group(1)
                            if spool_id_str not in active_ids:
                                try:
                                    if not dry_run:
                                        os.remove(os.path.join(fil_dir, filename))
                                    modified_any = True
                                except Exception:
                                    pass
    except Exception:
        pass
    return synced_count, modified_any

def auto_inject_presets():
    injected_count = 0
    try:
        if not os.path.exists(USER_DIR):
            return 0
            
        for root, dirs, files in os.walk(USER_DIR):
            for file in files:
                if file.endswith(".json"):
                    filepath = os.path.join(root, file)
                    is_preset = any(folder in filepath for folder in ["/machine/", "/filament/", "/process/"])
                    if not is_preset:
                        continue
                        
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            
                        if inject_plugin_metadata(data):
                            with open(filepath, "w", encoding="utf-8") as f:
                                json.dump(data, f, indent=4)
                            injected_count += 1
                    except Exception:
                        pass
    except Exception:
        pass
    return injected_count

def ensure_spoolman_fields(dry_run=False):
    created_count = 0
    try:
        settings = get_settings()
        url = settings["spoolman_url"].rstrip('/')
        
        # Get existing filament extra fields
        r = requests.get(f"{url}/api/v1/field/filament", timeout=5)
        if r.status_code != 200:
            return 0
        existing_keys = {field["key"] for field in r.json()}
        
        # Clean up range, cost & printer fields if they exist (we inherit printer compatibility now)
        for key in ["nozzle_temperature_range_low", "nozzle_temperature_range_high", "filament_cost", "compatible_printers"]:
            if key in existing_keys:
                if not dry_run:
                    requests.delete(f"{url}/api/v1/field/filament/{key}", timeout=5)
        
        required_fields = {
            "max_volumetric_speed": {"name": "Max Volumetric Speed", "field_type": "float", "unit": "mm\u00b3/s"}
        }
        
        for key, params in required_fields.items():
            if key not in existing_keys:
                if not dry_run:
                    # POST to create the field in Spoolman
                    res = requests.post(f"{url}/api/v1/field/filament/{key}", json=params, timeout=5)
                    if res.status_code == 200:
                        created_count += 1
                else:
                    created_count += 1
    except Exception:
        pass
    return created_count

def sync_spoolman_processes(dry_run=False):
    created_count = 0
    modified_any = False
    try:
        if not os.path.exists(SYSTEM_DIR) or not os.path.exists(USER_DIR):
            return 0, False
            
        # 1. Find all user subdirs
        user_subdirs = get_user_subdirs()

        # 2. Gather user's active/configured base printer model names
        active_printers = set()
        for subdir in user_subdirs:
            mach_dir = os.path.join(subdir, "machine")
            if os.path.exists(mach_dir):
                for file in os.listdir(mach_dir):
                    if file.endswith(".json"):
                        filepath = os.path.join(mach_dir, file)
                        try:
                            with open(filepath, "r", encoding="utf-8") as f:
                                mdata = json.load(f)
                            if mdata.get("name"):
                                active_printers.add(mdata["name"])
                            if mdata.get("inherits"):
                                active_printers.add(mdata["inherits"])
                            active_printers.add(file[:-5])
                        except Exception:
                            pass

        # 3. Find all system process presets compatible with user's active printers
        system_presets = {}
        if os.path.exists(SYSTEM_DIR):
            for root, dirs, files in os.walk(SYSTEM_DIR):
                if "process" in root:
                    for file in files:
                        if file.endswith(".json"):
                            filepath = os.path.join(root, file)
                            try:
                                with open(filepath, "r", encoding="utf-8") as f:
                                    data = json.load(f)
                                if data.get("instantiation") == "true" and data.get("name"):
                                    # Check compatibility
                                    compat = data.get("compatible_printers")
                                    if compat:
                                        # Check intersection with active printers
                                        if not any(p in active_printers for p in compat):
                                            continue # skip incompatible presets
                                    system_presets[data["name"]] = (file, data)
                            except Exception:
                                pass
                            
        # 4. Generate SpoolMan versions of the process presets
        for name, (filename, parent_data) in system_presets.items():
            preset_name = f"{name} - SpoolMan"
            preset_data = {}
            if parent_data:
                preset_data.update(parent_data)
                
            preset_data["type"] = "process"
            preset_data["name"] = preset_name
            preset_data["print_settings_id"] = preset_name
            preset_data["inherits"] = name
            preset_data["from"] = "User"
            preset_data["version"] = parent_data.get("version", "2.4.0.3")
            
            # Ensure plugin hooks are injected
            inject_plugin_metadata(preset_data)
            
            base_filename = filename[:-5]
            user_filename = f"{base_filename} - SpoolMan.json"
            
            for subdir in user_subdirs:
                proc_dir = os.path.join(subdir, "process")
                os.makedirs(proc_dir, exist_ok=True)
                filepath = os.path.join(proc_dir, user_filename)
                
                needs_write = True
                if os.path.exists(filepath):
                    try:
                        with open(filepath, "r", encoding="utf-8") as existing_f:
                            existing_data = json.load(existing_f)
                            if existing_data == preset_data:
                                needs_write = False
                    except Exception:
                        pass
                
                if needs_write:
                    if not dry_run:
                        with open(filepath, "w", encoding="utf-8") as f:
                            json.dump(preset_data, f, indent=4)
                    modified_any = True
                    created_count += 1

        # 5. Clean up orphaned SpoolMan process presets
        active_spoolman_filenames = {f"{filename[:-5]} - SpoolMan.json" for filename, _ in system_presets.values()}
        for subdir in user_subdirs:
            proc_dir = os.path.join(subdir, "process")
            if os.path.exists(proc_dir):
                for filename in os.listdir(proc_dir):
                    if filename.endswith(" - SpoolMan.json"):
                        if filename not in active_spoolman_filenames:
                            try:
                                if not dry_run:
                                    os.remove(os.path.join(proc_dir, filename))
                                modified_any = True
                            except Exception:
                                pass
    except Exception:
        pass
    return created_count, modified_any

def run_cleanup_logic():
    try:
        # 1. Clean up generated preset files and injections
        if os.path.exists(USER_DIR):
            for root, dirs, files in os.walk(USER_DIR):
                for file in files:
                    filepath = os.path.join(root, file)
                    
                    # Delete synced filaments
                    is_spoolman_filament = file.startswith("[Spool #") or file.startswith("Spoolman_Spool_") or file.endswith(" - Spoolman.json") or file.startswith("#")
                    if is_spoolman_filament and file.endswith(".json"):
                        try:
                            os.remove(filepath)
                        except Exception:
                            pass
                            
                    # Delete synced processes
                    elif file.endswith(" - SpoolMan.json"):
                        try:
                            os.remove(filepath)
                        except Exception:
                            pass

                    # Clean up auto-injected settings in other files
                    elif file.endswith(".json"):
                        try:
                            with open(filepath, "r", encoding="utf-8") as f:
                                data = json.load(f)
                            
                            modified = False
                            
                            if "plugins" in data:
                                plugins = data["plugins"]
                                if isinstance(plugins, list):
                                    if "spoolman_bridge;;Filament Usage Updater" in plugins:
                                        plugins.remove("spoolman_bridge;;Filament Usage Updater")
                                        modified = True
                                    if not plugins:
                                        del data["plugins"]
                                        modified = True
                                elif plugins == "spoolman_bridge;;Filament Usage Updater":
                                    del data["plugins"]
                                    modified = True

                            if "slicing_pipeline_plugin" in data:
                                pipeline = data["slicing_pipeline_plugin"]
                                if isinstance(pipeline, list):
                                    if "Filament Usage Updater" in pipeline:
                                        pipeline.remove("Filament Usage Updater")
                                        modified = True
                                    if not pipeline:
                                        del data["slicing_pipeline_plugin"]
                                        modified = True
                                elif pipeline == "Filament Usage Updater":
                                    del data["slicing_pipeline_plugin"]
                                    modified = True
                                    
                            if modified:
                                with open(filepath, "w", encoding="utf-8") as f:
                                    json.dump(data, f, indent=4)
                        except Exception:
                            pass
    except Exception:
        pass

def show_tutorial_dialog():
    settings = get_settings()
    current_url = settings.get("spoolman_url", "http://localhost:7912")
    
    html_content = f"""<!DOCTYPE html>
<html>
<head>
<style>
html, body {{
    font-family: var(--orca-font, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif);
    background-color: var(--orca-bg, #1e1e1e);
    color: var(--orca-fg, #f5f5f5);
    margin: 0;
    padding: 20px 24px;
    box-sizing: border-box;
    overflow: hidden;
    height: 100%;
    display: flex;
    flex-direction: column;
}}
.header {{
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 16px;
    border-bottom: 1px solid var(--orca-border, #333);
    padding-bottom: 8px;
    flex-shrink: 0;
}}
.logo {{
    font-size: 24px;
}}
h2 {{
    margin: 0;
    color: var(--orca-accent, #00adb5);
    font-size: 18px;
}}
.content {{
    flex-grow: 1;
    overflow-y: auto;
    margin-bottom: 16px;
}}
.steps {{
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-bottom: 16px;
}}
.step {{
    display: flex;
    gap: 12px;
}}
.step-num {{
    background-color: var(--orca-accent, #00adb5);
    color: var(--orca-accent-fg, #fff);
    font-weight: bold;
    border-radius: 50%;
    width: 20px;
    height: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    font-size: 11px;
}}
.step-text h4 {{
    margin: 0 0 2px 0;
    font-size: 13px;
}}
.step-text p {{
    margin: 0;
    font-size: 12px;
    color: var(--orca-muted, #ccc);
    line-height: 1.35;
}}
.divider {{
    border-top: 1px solid var(--orca-border, #333);
    margin: 16px 0;
}}
.settings-section h4 {{
    margin: 0 0 8px 0;
    font-size: 13px;
    color: var(--orca-accent, #00adb5);
}}
input {{
    width: 100%;
    background-color: rgba(255, 255, 255, 0.05);
    border: 1px solid var(--orca-border, #444);
    border-radius: 4px;
    color: var(--orca-fg, #fff);
    padding: 8px 12px;
    font-size: 13px;
    box-sizing: border-box;
    margin-top: 4px;
}}
input:focus {{
    outline: none;
    border-color: var(--orca-accent, #00adb5);
}}
.buttons {{
    display: flex;
    justify-content: flex-end;
    gap: 12px;
    flex-shrink: 0;
    border-top: 1px solid var(--orca-border, #333);
    padding-top: 12px;
}}
button {{
    background-color: var(--orca-accent, #00adb5);
    color: var(--orca-accent-fg, #fff);
    border: none;
    border-radius: 4px;
    padding: 8px 16px;
    font-size: 13px;
    cursor: pointer;
    font-weight: bold;
}}
button.secondary {{
    background-color: rgba(255, 255, 255, 0.1);
    color: var(--orca-fg, #fff);
}}
</style>
</head>
<body>
    <div class="header">
        <span class="logo">🔌</span>
        <h2>Spoolman Bridge Settings & Guide</h2>
    </div>
    
    <div class="content">
        <div class="steps">
            <div class="step">
                <div class="step-num">1</div>
                <div class="step-text">
                    <h4>1. Spool Filament Presets</h4>
                    <p>The plugin automatically creates a unique custom filament preset for each active spool in Spoolman (named <code>Vendor Filament (#ID) - Spoolman</code>).</p>
                </div>
            </div>
            <div class="step">
                <div class="step-num">2</div>
                <div class="step-text">
                    <h4>2. Duplicate Slicing Profiles</h4>
                    <p>It copies all your active process presets (slicing profiles) and appends <code>- SpoolMan</code> to them.</p>
                </div>
            </div>
            <div class="step">
                <div class="step-num">3</div>
                <div class="step-text">
                    <h4>3. Selection Required to Activate</h4>
                    <p>You <strong>must</strong> select both a synced Spoolman filament <strong>AND</strong> a synced <code>- SpoolMan</code> process preset when slicing. This activates the Filament Usage Updater to deduct printed weight automatically!</p>
                </div>
            </div>
        </div>
        
        <div class="divider"></div>
        
        <div class="settings-section">
            <h4>Configure Spoolman URL</h4>
            <p style="margin: 0; font-size: 12px; color: var(--orca-muted, #ccc);">Enter the URL of your self-hosted Spoolman instance:</p>
            <input type="text" id="spoolman_url" value="{current_url}" placeholder="http://localhost:7912">
        </div>
    </div>
    
    <div class="buttons">
        <button class="secondary" onclick="window.orca.submit({{}})">Cancel</button>
        <button onclick="submitForm()">Save & Close</button>
    </div>
    
    <script>
    function submitForm() {{
        var val = document.getElementById('spoolman_url').value.trim();
        window.orca.submit({{url: val}});
    }}
    </script>
</body>
</html>"""
    return orca.host.ui.show_dialog(html=html_content, title="Spoolman Settings & Guide", width=560, height=500)

def delayed_startup_check():
    # Wait for OrcaSlicer to finish loading and dismiss the splash screen
    time.sleep(6.0)
    try:
        settings = get_settings()
        
        # Check first run
        if settings.get("first_run", True):
            result = show_tutorial_dialog()
            if result and result.get("url"):
                settings["spoolman_url"] = result["url"]
            try:
                with open(SETTINGS_FILE, "w") as f:
                    json.dump({"spoolman_url": settings.get("spoolman_url", "http://localhost:7912"), "first_run": False}, f, indent=4)
            except Exception:
                pass
                
        url = settings["spoolman_url"].rstrip('/')
        
        # Test connection first!
        connection_failed = False
        try:
            r = requests.get(f"{url}/api/v1/spool?allow_archived=false", timeout=3)
            if r.status_code != 200:
                connection_failed = True
        except Exception:
            connection_failed = True
            
        if connection_failed:
            choice = orca.host.ui.message(
                f"Could not connect to Spoolman at:\n{url}\n\nDo you want to configure the Spoolman URL?",
                title="Spoolman Bridge Connection Error",
                buttons="yes_no",
                icon="warning"
            )
            if choice == "yes":
                config_script = SpoolmanConfigScript()
                config_script.execute()
            return

        # Check if fields require updating in Spoolman (dry-run check)
        created_fields = ensure_spoolman_fields(dry_run=True)
        # Check if filaments or processes require syncing (dry-run checks)
        sync_count, modified_fils = sync_spoolman_filaments(dry_run=True)
        created_procs, modified_procs = sync_spoolman_processes(dry_run=True)
        
        modified = modified_fils or modified_procs or (created_fields > 0)
        
        if modified:
            choice = orca.host.ui.message(
                "New Spoolman profiles or settings changes detected.\n\nDo you want to sync them now?",
                title="Spoolman Bridge",
                buttons="yes_no",
                icon="question"
            )
            if choice == "yes":
                count = auto_inject_presets()
                real_created_fields = ensure_spoolman_fields(dry_run=False)
                real_sync_count, _ = sync_spoolman_filaments(dry_run=False)
                real_created_procs, _ = sync_spoolman_processes(dry_run=False)
                
                orca.host.ui.message("Spoolman spools & processes synced. Please restart OrcaSlicer to load them.", title="Spoolman Bridge", icon="info")
                
                write_log(f"[SYNCED] Spoolman spools & processes synced successfully. Auto-injected to {count} presets. Created {real_created_fields} fields. Synced {real_sync_count} filaments, {real_created_procs} process presets.")
            else:
                write_log("[INFO] User skipped startup sync.")
        else:
            write_log("[INFO] Startup sync check: already up-to-date.")
    except Exception as e:
        write_log(f"[EXCEPTION] delayed_startup_check failed: {e}")

class SpoolmanGCode(orca.slicing.SlicingPipelineCapabilityBase):
    def get_name(self):
        return "Filament Usage Updater"

    def on_load(self):
        try:
            settings = get_settings()
            if settings.get("cleanup") or settings.get("run_cleanup"):
                run_cleanup_logic()
                
                # Reset settings to prevent infinite loop
                try:
                    with open(SETTINGS_FILE, "w") as f:
                        json.dump({"spoolman_url": settings.get("spoolman_url", "http://localhost:7912")}, f, indent=4)
                except Exception:
                    pass
                    
                orca.host.ui.message("Spoolman Bridge: Cleanup completed! Please restart OrcaSlicer.", title="Spoolman Bridge", icon="info")
                return

            # Start startup check in a background thread to prevent blocking the splash screen / main thread!
            threading.Thread(target=delayed_startup_check, daemon=True).start()
            
        except Exception as e:
            write_log(f"[EXCEPTION] on_load failed: {e}")

    def execute(self, ctx):
        settings = get_settings()
        url = settings["spoolman_url"].rstrip('/')

        step = getattr(ctx, "step", None)
        write_log(f"[INVOKED] Slicing step detected. step={step}, path={ctx.gcode_path}, output={ctx.output_name}")

        # 1. Skip if this is not the G-code post-processing step
        if step is not None and step != orca.slicing.psGCodePostProcess:
            write_log(f"[SKIPPED] Non-GCode post-process step: {step}")
            return orca.ExecutionResult.skipped("Not in G-code post-process step")

        # 2. Skip if G-code file path is not ready or file doesn't exist yet
        if not ctx.gcode_path or not os.path.exists(ctx.gcode_path):
            write_log(f"[SKIPPED] G-code file path is empty or not found yet: {ctx.gcode_path}")
            return orca.ExecutionResult.skipped("G-code file not ready")

        # Parse G-code for filament consumption & metadata
        def _split(v, sep):
            return [x.strip().strip('"').strip() for x in v.split(sep)]

        used = []
        presets, types, vendors, colours = [], [], [], []
        try:
            file_size = os.path.getsize(ctx.gcode_path)
            seek_pos = max(0, file_size - 400000)
            with open(ctx.gcode_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(seek_pos)
                for line in f:
                    s = line.strip()
                    if not s.startswith(";") or "=" not in s:
                        continue
                    key, _, val = s.lstrip(";").partition("=")
                    key, val = key.strip(), val.strip()
                    if key == "filament used [g]":
                        vals = []
                        for x in val.split(","):
                            try:
                                vals.append(float(x.strip()))
                            except ValueError:
                                vals.append(0.0)
                        used = vals
                    elif key == "filament_settings_id":
                        presets = _split(val, ";")
                    elif key == "filament_type":
                        types = _split(val, ";")
                    elif key == "filament_vendor":
                        vendors = _split(val, ";")
                    elif key == "filament_colour":
                        colours = _split(val, ";")
            write_log(f"[PARSED] used={used}, presets={presets}, types={types}, vendors={vendors}, colours={colours}")
        except Exception as e:
            msg = f"Failed to parse G-code: {e}"
            write_log(f"[ERROR] {msg}")
            return orca.ExecutionResult.failure(orca.PluginResult.FatalError, msg)

        def _at(lst, i):
            if not lst:
                return ""
            return lst[i] if i < len(lst) else lst[0]

        slots = [(i, g) for i, g in enumerate(used) if g > 0.0]
        if not slots:
            msg = "No filament usage detected in G-code."
            write_log(f"[SKIPPED] {msg}")
            return orca.ExecutionResult.skipped(msg)

        # Query Spoolman active spools
        try:
            r = requests.get(f"{url}/api/v1/spool?allow_archived=false", timeout=5)
            if r.status_code != 200:
                msg = f"Failed to query Spoolman, status {r.status_code}"
                write_log(f"[ERROR] {msg}")
                return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, msg)
            spools = r.json()
        except Exception as e:
            msg = f"Error connecting to Spoolman: {e}"
            write_log(f"[ERROR] {msg}")
            return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, msg)

        by_id = {s.get("id"): s for s in spools}
        deductions = {}   # spool_id -> [spool, grams, [slot numbers]]
        unmatched = []
        for i, g in slots:
            preset = _at(presets, i)
            spool, score = None, 0
            m = re.search(r"\(#(\d+)\)", preset)
            if m and int(m.group(1)) in by_id:
                spool, score = by_id[int(m.group(1))], 100
            else:
                spool, score = find_best_spool(spools, _at(vendors, i), _at(types, i), _at(colours, i), preset)
            if not spool or score <= 10:
                unmatched.append(f"slot {i + 1} ({preset or 'unknown'}, {g:.2f}g)")
                write_log(f"[NO MATCH] slot {i + 1}: preset={preset} used={g}")
                continue
            entry = deductions.setdefault(spool.get("id"), [spool, 0.0, []])
            entry[1] += g
            entry[2].append(i + 1)
            write_log(f"[MATCHED] slot {i + 1} -> spool #{spool.get('id')} score={score} used={g}")

        if not deductions:
            msg = "No matching spools found in Spoolman for: " + ", ".join(unmatched)
            write_log(f"[SKIPPED] {msg}")
            return orca.ExecutionResult.skipped(msg)

        def _desc(spool):
            fil = spool.get("filament") or {}
            return f"{(fil.get('vendor') or {}).get('name', 'Generic')} {fil.get('name', 'Unnamed')}"

        lines = [f"- {round(g, 2)}g from spool #{sid} ({_desc(sp)}) [slot {', '.join(map(str, sl))}]"
                 for sid, (sp, g, sl) in deductions.items()]
        question = "Remove filament usage from Spoolman?\n\n" + "\n".join(lines)
        if unmatched:
            question += "\n\nNo spool matched (skipped): " + ", ".join(unmatched)
        choice = orca.host.ui.message(question, title="Spoolman Bridge", buttons="yes_no", icon="question")
        if choice != "yes":
            msg = "User skipped filament deduction."
            write_log(f"[SKIPPED] {msg}")
            return orca.ExecutionResult.skipped(msg)

        done, failed = [], []
        for sid, (sp, g, sl) in deductions.items():
            payload = {"use_weight": round(g, 2)}
            try:
                write_log(f"[API PUT] {payload} -> {url}/api/v1/spool/{sid}/use")
                r = requests.put(f"{url}/api/v1/spool/{sid}/use", json=payload, timeout=10)
                if r.status_code == 200:
                    done.append(f"{round(g, 2)}g from #{sid}")
                else:
                    failed.append(f"#{sid}: HTTP {r.status_code} {r.text[:200]}")
            except Exception as e:
                failed.append(f"#{sid}: {e}")
        if failed:
            msg = "Deducted " + (", ".join(done) or "nothing") + ". Failed: " + "; ".join(failed)
            write_log(f"[API ERROR] {msg}")
            return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, msg)
        msg = "Deducted " + ", ".join(done)
        write_log(f"[SUCCESS] {msg}")
        return orca.ExecutionResult.success(msg)

class SpoolmanCleanupScript(orca.script.ScriptPluginCapabilityBase):
    def get_name(self):
        return "Cleanup Spoolman Files"

    def execute(self):
        try:
            run_cleanup_logic()
            orca.host.ui.message("Spoolman Bridge: Cleanup completed! Please restart OrcaSlicer.", title="Spoolman Bridge", icon="info")
            return orca.ExecutionResult.success("Cleanup completed")
        except Exception as e:
            return orca.ExecutionResult.failure(orca.PluginResult.FatalError, f"Cleanup failed: {e}")

class SpoolmanSyncScript(orca.script.ScriptPluginCapabilityBase):
    def get_name(self):
        return "Sync Spoolman Profiles"

    def execute(self):
        try:
            settings = get_settings()
            url = settings["spoolman_url"].rstrip('/')
            
            # Test connection first!
            connection_failed = False
            error_msg = ""
            try:
                r = requests.get(f"{url}/api/v1/spool?allow_archived=false", timeout=5)
                if r.status_code != 200:
                    connection_failed = True
                    error_msg = f"status code {r.status_code}"
            except Exception as e:
                connection_failed = True
                error_msg = str(e)
                
            if connection_failed:
                choice = orca.host.ui.message(
                    f"Could not connect to Spoolman at:\n{url}\n\nError: {error_msg}\n\nDo you want to configure the Spoolman URL?",
                    title="Spoolman Bridge Connection Error",
                    buttons="yes_no",
                    icon="warning"
                )
                if choice == "yes":
                    config_script = SpoolmanConfigScript()
                    config_script.execute()
                return orca.ExecutionResult.skipped("Connection failed")

            # Check if fields require updating in Spoolman (dry-run check)
            created_fields = ensure_spoolman_fields(dry_run=True)
            # Check if filaments or processes require syncing (dry-run checks)
            sync_count, modified_fils = sync_spoolman_filaments(dry_run=True)
            created_procs, modified_procs = sync_spoolman_processes(dry_run=True)
            
            modified = modified_fils or modified_procs or (created_fields > 0)
            
            if modified:
                count = auto_inject_presets()
                real_created_fields = ensure_spoolman_fields(dry_run=False)
                real_sync_count, _ = sync_spoolman_filaments(dry_run=False)
                real_created_procs, _ = sync_spoolman_processes(dry_run=False)
                
                orca.host.ui.message("Spoolman spools & processes synced successfully. Please restart OrcaSlicer to load them.", title="Spoolman Bridge", icon="info")
            else:
                orca.host.ui.message("Spoolman is already up-to-date! No changes needed.", title="Spoolman Bridge", icon="info")
                
            return orca.ExecutionResult.success("Sync completed")
        except Exception as e:
            return orca.ExecutionResult.failure(orca.PluginResult.FatalError, f"Sync failed: {e}")

class SpoolmanConfigScript(orca.script.ScriptPluginCapabilityBase):
    def get_name(self):
        return "Spoolman Settings & Guide"

    def execute(self):
        result = show_tutorial_dialog()
        if result and result.get("url"):
            new_url = result["url"]
            try:
                with open(SETTINGS_FILE, "w") as f:
                    json.dump({"spoolman_url": new_url, "first_run": False}, f, indent=4)
                
                # Test connection of the new URL
                url_test = new_url.rstrip('/')
                connection_failed = False
                error_msg = ""
                try:
                    r = requests.get(f"{url_test}/api/v1/spool?allow_archived=false", timeout=5)
                    if r.status_code != 200:
                        connection_failed = True
                        error_msg = f"status code {r.status_code}"
                except Exception as e:
                    connection_failed = True
                    error_msg = str(e)

                if connection_failed:
                    orca.host.ui.message(
                        f"URL saved, but connection failed to:\n{new_url}\n\nError: {error_msg}\n\nPlease check Spoolman status.",
                        title="Spoolman Connection Warning",
                        icon="warning"
                    )
                else:
                    # Connection succeeded! Check if anything needs syncing
                    created_fields = ensure_spoolman_fields(dry_run=True)
                    sync_count, modified_fils = sync_spoolman_filaments(dry_run=True)
                    created_procs, modified_procs = sync_spoolman_processes(dry_run=True)
                    
                    modified = modified_fils or modified_procs or (created_fields > 0)
                    
                    if modified:
                        choice = orca.host.ui.message(
                            "Connection successful!\n\nNew Spoolman profiles or settings changes detected. Do you want to sync them now?",
                            title="Spoolman Bridge",
                            buttons="yes_no",
                            icon="question"
                        )
                        if choice == "yes":
                            count = auto_inject_presets()
                            real_created_fields = ensure_spoolman_fields(dry_run=False)
                            real_sync_count, _ = sync_spoolman_filaments(dry_run=False)
                            real_created_procs, _ = sync_spoolman_processes(dry_run=False)
                            
                            orca.host.ui.message("Spoolman spools & processes synced successfully. Please restart OrcaSlicer to load them.", title="Spoolman Bridge", icon="info")
                    else:
                        orca.host.ui.message("Connection successful! Spoolman profiles are already up-to-date.", title="Spoolman Bridge", icon="info")
            except Exception as e:
                orca.host.ui.message(f"Failed to save settings: {e}", title="Spoolman Bridge", icon="error")
        return orca.ExecutionResult.success("Settings closed")

@orca.plugin
class SpoolmanPlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(SpoolmanGCode)
        orca.register_capability(SpoolmanCleanupScript)
        orca.register_capability(SpoolmanSyncScript)
        orca.register_capability(SpoolmanConfigScript)
