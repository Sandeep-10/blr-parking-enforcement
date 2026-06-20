import pandas as pd
import numpy as np
import json
import os
import time
import math
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

t_start = time.time()

# Paths
csv_path = r"C:\Users\chang\Downloads\Flipkart hackathon\Prototype\jan to may police violation_anonymized791b166.csv"
output_js_path = r"C:\Users\chang\Downloads\Flipkart hackathon\Prototype\dashboard_data.js"
dist_js_path = r"C:\Users\chang\Downloads\Flipkart hackathon\Prototype\dist\dashboard_data.js"
cache_dir = r"C:\Users\chang\.gemini\antigravity\brain\10c96675-7041-4ba8-99d5-f5d8624bd886\scratch"
pois_cache_path = os.path.join(cache_dir, "pois.json")
ways_cache_path = os.path.join(cache_dir, "ways.json")

# Bounding box & grid size
lat_min, lat_max = 12.80, 13.30
lon_min, lon_max = 77.40, 77.80
grid_size = 0.0005  # ~56m resolution

# 1. Load and Clean Dataset
print("Loading dataset...")
df = pd.read_csv(csv_path, usecols=[
    'latitude', 'longitude', 'location', 'vehicle_number', 'vehicle_type',
    'violation_type', 'created_datetime', 'police_station', 'junction_name',
    'validation_status'
])
print(f"Loaded {len(df)} rows in {time.time() - t_start:.2f}s")

df = df.dropna(subset=['latitude', 'longitude'])
df = df[(df['latitude'] >= lat_min) & (df['latitude'] <= lat_max) & 
        (df['longitude'] >= lon_min) & (df['longitude'] <= lon_max)]
print(f"Rows after bounding box filter: {len(df)}")

df['junction_name'] = df['junction_name'].fillna('No Junction').astype(str).str.strip()
df['vehicle_type'] = df['vehicle_type'].fillna('OTHERS').astype(str).str.strip().str.upper()
df['police_station'] = df['police_station'].fillna('Unknown').astype(str).str.strip()

# Normalization
def get_validation_status_clean(status):
    status = str(status).lower().strip()
    if status == 'approved':
        return 'Approved'
    elif status == 'rejected':
        return 'Rejected'
    else:
        return 'Pending Review'
df['status'] = df['validation_status'].apply(get_validation_status_clean)

def get_first_violation(val):
    try:
        arr = json.loads(val)
        if isinstance(arr, list) and len(arr) > 0:
            return arr[0]
    except Exception:
        pass
    return str(val)
df['first_violation'] = df['violation_type'].apply(get_first_violation)

# Standardize categories
top_vehicles = ['SCOOTER', 'CAR', 'MOTOR CYCLE', 'PASSENGER AUTO', 'MAXI-CAB']
df['vehicle_cat'] = df['vehicle_type']
df.loc[~df['vehicle_cat'].isin(top_vehicles), 'vehicle_cat'] = 'OTHERS'

top_violations = ['WRONG PARKING', 'NO PARKING', 'PARKING IN A MAIN ROAD', 'DEFECTIVE NUMBER PLATE', 'PARKING ON FOOTPATH']
df['violation_cat'] = df['first_violation'].astype(str).str.strip().str.upper()
df.loc[~df['violation_cat'].isin(top_violations), 'violation_cat'] = 'OTHERS'

df['dt_utc'] = pd.to_datetime(df['created_datetime'], errors='coerce')
df['dt_ist'] = df['dt_utc'].dt.tz_convert('Asia/Kolkata')
df['date_ist'] = df['dt_ist'].dt.date
df['hour'] = df['dt_ist'].dt.hour
df['day_of_week'] = df['dt_ist'].dt.dayofweek

print("Deduplicating rows...")
df['vehicle_number_temp'] = df['vehicle_number'].fillna('UNKNOWN')
df = df.drop_duplicates(subset=['latitude', 'longitude', 'vehicle_number_temp', 'created_datetime'])
df = df.drop(columns=['vehicle_number_temp'])
print(f"Remaining rows: {len(df)}")

# Create grid centroids
df['lat_grid'] = np.round(df['latitude'] / grid_size) * grid_size
df['lon_grid'] = np.round(df['longitude'] / grid_size) * grid_size

# Group into hotspots (count >= 5)
hotspot_counts = df.groupby(['lat_grid', 'lon_grid']).size().reset_index(name='violations_count')
hotspots = hotspot_counts[hotspot_counts['violations_count'] >= 5].copy()
print(f"Total hotspots: {len(hotspots)}")

# Set index map
hotspots = hotspots.sort_values(by='violations_count', ascending=False).reset_index(drop=True)
hotspots['id'] = hotspots.index
hotspot_map = {(row['lat_grid'], row['lon_grid']): row['id'] for idx, row in hotspots.iterrows()}

# ==========================================================
# TIME-PATTERN PREDICTOR & VALIDATION Setup
# ==========================================================
print("\n--- Running Time-Series Validation Setup ---")
# Split Date: Train before 2024-03-11, Test from 2024-03-11 onwards (last 4 weeks)
split_date = pd.to_datetime("2024-03-11").date()
df_train = df[df['date_ist'] < split_date].copy()
df_test = df[df['date_ist'] >= split_date].copy()

print(f"Training set span: {df_train['date_ist'].min()} to {df_train['date_ist'].max()} ({len(df_train)} records)")
print(f"Test set span: {df_test['date_ist'].min()} to {df_test['date_ist'].max()} ({len(df_test)} records)")

# 1. Count occurrences of each weekday in the training period
train_dates = pd.DataFrame({'date': pd.date_range(df_train['date_ist'].min(), df_train['date_ist'].max())})
train_dates['day_of_week'] = train_dates['date'].dt.dayofweek
train_weekday_counts = train_dates.groupby('day_of_week').size().to_dict()
print(f"Weekday training occurrences: {train_weekday_counts}")

# 2. Count occurrences of each weekday in the test period
test_dates = pd.DataFrame({'date': pd.date_range(df_test['date_ist'].min(), df_test['date_ist'].max())})
test_dates['day_of_week'] = test_dates['date'].dt.dayofweek
test_weekday_counts = test_dates.groupby('day_of_week').size().to_dict()
print(f"Weekday test occurrences: {test_weekday_counts}")

# 3. Model: Compute Historical Average rates for (hotspot, day_of_week, hour)
# We aggregate training counts
train_grouped = df_train.groupby(['lat_grid', 'lon_grid', 'day_of_week', 'hour']).size().reset_index(name='train_count')

# For each hotspot, compute the full 7x24 matrix
hotspot_prediction_matrices = {}
hotspot_thin_flags = {}

# Active weeks per hotspot
hotspot_weeks = df_train.groupby(['lat_grid', 'lon_grid', 'date_ist']).size().reset_index(name='c')
hotspot_active_weeks_count = hotspot_weeks.groupby(['lat_grid', 'lon_grid'])['date_ist'].nunique().reset_index(name='active_days')

for idx, row in hotspots.iterrows():
    lat, lon = row['lat_grid'], row['lon_grid']
    h_idx = row['id']
    
    # Check minimum data threshold (at least 5 active days/weeks in train set)
    active_days_df = hotspot_active_weeks_count[(hotspot_active_weeks_count['lat_grid'] == lat) & (hotspot_active_weeks_count['lon_grid'] == lon)]
    active_days = int(active_days_df['active_days'].values[0]) if len(active_days_df) > 0 else 0
    
    # Flag thinly sampled if active days < 6 (approx 1 per month)
    is_thin = active_days < 6
    hotspot_thin_flags[h_idx] = is_thin
    
    # Initialize matrix
    matrix = np.zeros((7, 24))
    
    # Fill from train counts
    subset = train_grouped[(train_grouped['lat_grid'] == lat) & (train_grouped['lon_grid'] == lon)]
    for _, s_row in subset.iterrows():
        dow = int(s_row['day_of_week'])
        h = int(s_row['hour'])
        cnt = float(s_row['train_count'])
        
        # Divide by how many times this weekday occurred in training to get expected hourly rate
        divisor = train_weekday_counts.get(dow, 1)
        matrix[dow, h] = cnt / divisor
        
    hotspot_prediction_matrices[h_idx] = matrix

# 4. Validation Evaluation
# We evaluate predictions on the test set.
# Aggregate test counts by (hotspot, date, hour)
test_actuals = df_test.groupby(['lat_grid', 'lon_grid', 'date_ist', 'day_of_week', 'hour']).size().reset_index(name='actual_count')

# For evaluation, we need to compare actual counts vs predicted counts for all hours in the test set.
# Let's align them:
mae_list = []
mae_list_regular = []
mae_list_thin = []

# Naive baseline: predict overall average count for the hotspot
overall_train_counts = df_train.groupby(['lat_grid', 'lon_grid']).size().reset_index(name='total_train')
overall_train_counts['overall_avg'] = overall_train_counts['total_train'] / len(train_dates)
overall_avg_dict = {(row['lat_grid'], row['lon_grid']): row['overall_avg'] for _, row in overall_train_counts.iterrows()}

naive_mae_list = []

for _, row in test_actuals.iterrows():
    lat, lon = row['lat_grid'], row['lon_grid']
    if (lat, lon) not in hotspot_map:
        continue
    h_idx = hotspot_map[(lat, lon)]
    dow = int(row['day_of_week'])
    h = int(row['hour'])
    actual = float(row['actual_count'])
    
    # Prediction: Historical average hourly rate
    pred = hotspot_prediction_matrices[h_idx][dow, h]
    error = abs(actual - pred)
    mae_list.append(error)
    
    if hotspot_thin_flags[h_idx]:
        mae_list_thin.append(error)
    else:
        mae_list_regular.append(error)
        
    # Naive Prediction: overall daily average divided by 24
    overall_avg = overall_avg_dict.get((lat, lon), 0.0)
    naive_pred = overall_avg / 24.0
    naive_error = abs(actual - naive_pred)
    naive_mae_list.append(naive_error)

overall_mae = np.mean(mae_list) if mae_list else 0.0
regular_mae = np.mean(mae_list_regular) if mae_list_regular else 0.0
thin_mae = np.mean(mae_list_thin) if mae_list_thin else 0.0
naive_mae = np.mean(naive_mae_list) if naive_mae_list else 0.0

lift_pct = ((naive_mae - overall_mae) / naive_mae * 100.0) if naive_mae > 0 else 0.0

print("\n--- Validation Results ---")
print(f"Overall Test MAE: {overall_mae:.4f} violations/hour")
print(f"Regular Hotspots MAE: {regular_mae:.4f} violations/hour")
print(f"Thinly-Sampled Hotspots MAE: {thin_mae:.4f} violations/hour")
print(f"Naive Baseline MAE: {naive_mae:.4f} violations/hour")
print(f"Model Lift over Baseline: {lift_pct:.2f}% error reduction")
print(f"Thinly-sampled hotspots: {sum(hotspot_thin_flags.values())} out of {len(hotspots)} ({sum(hotspot_thin_flags.values())/len(hotspots):.1%})")

# ==========================================================
# RE-IMPORT CONGESTION METADATA FROM ORIGINAL PIPELINE
# ==========================================================
# Since we need to update dashboard_data.js, let's load congestion scoring details
# from the existing file so we don't lose OSM snapped roads and POIs.
# We will read HOTSPOTS_DATA, POLICE_STATIONS_DATA, and JUNCTIONS_DATA from the existing dashboard_data.js.
print("\nLoading existing database to preserve OSM and POI properties...")
try:
    with open(output_js_path, 'r', encoding='utf-8') as f:
        existing_js = f.read()
    
    # Parse out the JSON variables by slicing
    def parse_js_var(js_str, var_name):
        start_idx = js_str.find(f"const {var_name} = ") + len(f"const {var_name} = ")
        end_idx = js_str.find(";", start_idx)
        return json.loads(js_str[start_idx:end_idx])
    
    existing_hotspots = parse_js_var(existing_js, "HOTSPOTS_DATA")
    existing_ps = parse_js_var(existing_js, "POLICE_STATIONS_DATA")
    existing_junc = parse_js_var(existing_js, "JUNCTIONS_DATA")
    existing_records = parse_js_var(existing_js, "FILTER_RECORDS")
    
    print(f"Loaded {len(existing_hotspots)} hotspots from existing database.")
except Exception as ex:
    print(f"Error loading existing database: {ex}. Running complete regeneration instead...")
    # Fallback to loading existing variables or throw since we need the OSM/POI tags
    raise ex

# Update existing hotspots with predictions
updated_hotspots = []
for h in existing_hotspots:
    lat, lon = h['lat'], h['lon']
    h_idx = hotspot_map.get((lat, lon))
    
    if h_idx is not None:
        pred_matrix = hotspot_prediction_matrices[h_idx]
        is_thin = hotspot_thin_flags[h_idx]
        
        # Round rates to 3 decimals to save space
        h['weekly_pattern'] = np.round(pred_matrix, 3).tolist()
        h['thinly_sampled'] = bool(is_thin)
    else:
        # Fallback if hotspot index not mapped
        h['weekly_pattern'] = np.zeros((7, 24)).tolist()
        h['thinly_sampled'] = True
        
    updated_hotspots.append(h)

# Re-write the database file
js_content = f"""// Pre-processed dataset for Bengaluru Parking Enforcement Intelligence Dashboard with Congestion-Impact Scoring Layer
const CATEGORY_MAPS = {{
  hours: {list(range(24))},
  weekdays: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
  vehicles: ["SCOOTER", "CAR", "MOTOR CYCLE", "PASSENGER AUTO", "MAXI-CAB", "OTHERS"],
  violations: ["WRONG PARKING", "NO PARKING", "PARKING IN A MAIN ROAD", "DEFECTIVE NUMBER PLATE", "PARKING ON FOOTPATH", "OTHERS"],
  status: ["Approved", "Rejected", "Pending Review"]
}};

const HOTSPOTS_DATA = {json.dumps(updated_hotspots)};
const POLICE_STATIONS_DATA = {json.dumps(existing_ps)};
const JUNCTIONS_DATA = {json.dumps(existing_junc)};

// Grouped records array for real-time exact filtering in browser:
// Each record is: [hotspot_idx, hour, day_of_week, vehicle_cat_idx, violation_cat_idx, status_idx, count]
const FILTER_RECORDS = {json.dumps(existing_records)};

const SYSTEM_INFO = {{
  generated_at: "{pd.Timestamp.now().isoformat()}",
  total_raw_violations: {len(df)},
  global_avg_approval: {float(df[df['status']=='Approved'].shape[0] / (df[df['status']=='Approved'].shape[0] + df[df['status']=='Rejected'].shape[0]))},
  lat_bounds: [{lat_min}, {lat_max}],
  lon_bounds: [{lon_min}, {lon_max}],
  prediction_validation: {{
    split_date: "2024-03-11",
    overall_mae: {float(overall_mae)},
    regular_mae: {float(regular_mae)},
    thin_mae: {float(thin_mae)},
    baseline_mae: {float(naive_mae)},
    lift_pct: {float(lift_pct)}
  }}
}};
"""

# Write to both locations
print(f"Writing database to {output_js_path} ...")
with open(output_js_path, 'w', encoding='utf-8') as f:
    f.write(js_content)

print(f"Writing database to {dist_js_path} ...")
os.makedirs(os.path.dirname(dist_js_path), exist_ok=True)
with open(dist_js_path, 'w', encoding='utf-8') as f:
    f.write(js_content)

print(f"Database update complete! Saved in {time.time() - t_start:.2f}s.")
print(f"Output file size: {os.path.getsize(output_js_path)/1024/1024:.2f} MB")
