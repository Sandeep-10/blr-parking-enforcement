import pandas as pd
import numpy as np
import json
import os
import time

t_start = time.time()
file_path = r"C:\Users\chang\Downloads\Flipkart hackathon\Prototype\jan to may police violation_anonymized791b166.csv"
output_path = r"C:\Users\chang\Downloads\Flipkart hackathon\Prototype\dashboard_data.js"

print("Loading dataset...")
df = pd.read_csv(file_path, usecols=[
    'latitude', 'longitude', 'location', 'vehicle_number', 'vehicle_type',
    'violation_type', 'created_datetime', 'police_station', 'junction_name',
    'validation_status'
])
print(f"Loaded {len(df)} rows in {time.time() - t_start:.2f}s")

# === STEP 1: CLEANING ===
print("Cleaning data...")
df = df.dropna(subset=['latitude', 'longitude'])

# Bounding box filter (Bengaluru Metropolitan Area)
lat_min, lat_max = 12.80, 13.30
lon_min, lon_max = 77.40, 77.80
df = df[(df['latitude'] >= lat_min) & (df['latitude'] <= lat_max) & 
        (df['longitude'] >= lon_min) & (df['longitude'] <= lon_max)]
print(f"Rows after bounding box filter: {len(df)}")

df['junction_name'] = df['junction_name'].fillna('No Junction').astype(str).str.strip()
df['vehicle_type'] = df['vehicle_type'].fillna('OTHERS').astype(str).str.strip().str.upper()
df['police_station'] = df['police_station'].fillna('Unknown').astype(str).str.strip()

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

# Parse created_datetime as UTC and convert to Asia/Kolkata (IST)
df['dt_utc'] = pd.to_datetime(df['created_datetime'], errors='coerce')
df['dt_ist'] = df['dt_utc'].dt.tz_convert('Asia/Kolkata')
df['date_ist'] = df['dt_ist'].dt.date
df['hour'] = df['dt_ist'].dt.hour
df['day_of_week'] = df['dt_ist'].dt.dayofweek # 0-6

print("Deduplicating rows...")
df['vehicle_number_temp'] = df['vehicle_number'].fillna('UNKNOWN')
df = df.drop_duplicates(subset=['latitude', 'longitude', 'vehicle_number_temp', 'created_datetime'])
df = df.drop(columns=['vehicle_number_temp'])
print(f"Remaining rows: {len(df)}")

# === STEP 2: GRID CLUSTERING ===
print("Performing grid clustering...")
grid_size = 0.0005  # ~56m resolution
df['lat_grid'] = np.round(df['latitude'] / grid_size) * grid_size
df['lon_grid'] = np.round(df['longitude'] / grid_size) * grid_size

# Identify hotspots (count >= 5)
hotspot_counts = df.groupby(['lat_grid', 'lon_grid']).size().reset_index(name='violations_count')
hotspots = hotspot_counts[hotspot_counts['violations_count'] >= 5].copy()
print(f"Total hotspots (>=5): {len(hotspots)}")

# Calculate hotspot properties
print("Computing repeat offense rates...")
hotspot_daily = df.groupby(['lat_grid', 'lon_grid', 'date_ist']).size().reset_index(name='daily_count')
hotspot_max_daily = hotspot_daily.groupby(['lat_grid', 'lon_grid'])['daily_count'].max().reset_index(name='max_daily_count')
hotspots = hotspots.merge(hotspot_max_daily, on=['lat_grid', 'lon_grid'], how='left')
hotspots['repeat_offense_rate'] = (hotspots['violations_count'] - hotspots['max_daily_count']) / hotspots['violations_count']

print("Computing approval rates...")
hotspot_status = df.groupby(['lat_grid', 'lon_grid', 'status']).size().unstack(fill_value=0).reset_index()
for s in ['Approved', 'Rejected', 'Pending Review']:
    if s not in hotspot_status.columns:
        hotspot_status[s] = 0
hotspots = hotspots.merge(hotspot_status, on=['lat_grid', 'lon_grid'], how='left')
hotspots['approval_rate'] = hotspots['Approved'] / (hotspots['Approved'] + hotspots['Rejected'])

global_approved = df[df['status']=='Approved'].shape[0]
global_rejected = df[df['status']=='Rejected'].shape[0]
global_avg_approval = global_approved / (global_approved + global_rejected)
hotspots['approval_rate'] = hotspots['approval_rate'].fillna(global_avg_approval)

print("Finding dominant metadata for hotspots...")
def get_dominant_meta(df_subset, group_cols, target_col, name):
    counts = df_subset.groupby(group_cols + [target_col]).size().reset_index(name='c')
    idx = counts.groupby(group_cols)['c'].idxmax()
    return counts.loc[idx, group_cols + [target_col]].rename(columns={target_col: name})

dominant_loc = get_dominant_meta(df, ['lat_grid', 'lon_grid'], 'location', 'dominant_location')
dominant_junc = get_dominant_meta(df, ['lat_grid', 'lon_grid'], 'junction_name', 'dominant_junction')
dominant_ps = get_dominant_meta(df, ['lat_grid', 'lon_grid'], 'police_station', 'dominant_police_station')
dominant_veh = get_dominant_meta(df, ['lat_grid', 'lon_grid'], 'vehicle_cat', 'dominant_vehicle')
dominant_viol = get_dominant_meta(df, ['lat_grid', 'lon_grid'], 'violation_cat', 'dominant_violation')

hotspots = hotspots.merge(dominant_loc, on=['lat_grid', 'lon_grid'], how='left')
hotspots = hotspots.merge(dominant_junc, on=['lat_grid', 'lon_grid'], how='left')
hotspots = hotspots.merge(dominant_ps, on=['lat_grid', 'lon_grid'], how='left')
hotspots = hotspots.merge(dominant_veh, on=['lat_grid', 'lon_grid'], how='left')
hotspots = hotspots.merge(dominant_viol, on=['lat_grid', 'lon_grid'], how='left')

# === STEP 3: COMPOSITE RANKING SCORE ===
print("Calculating composite scores...")
hotspots['volume_score'] = np.minimum(100.0, (hotspots['violations_count'] / 500.0) * 100.0)
hotspots['approval_score'] = hotspots['approval_rate'] * 100.0
hotspots['repeat_score'] = hotspots['repeat_offense_rate'] * 100.0
hotspots['composite_score'] = (
    0.40 * hotspots['volume_score'] + 
    0.30 * hotspots['approval_score'] + 
    0.30 * hotspots['repeat_score']
)
hotspots = hotspots.sort_values(by='composite_score', ascending=False).reset_index(drop=True)
hotspots['id'] = hotspots.index

# Mapping of (lat_grid, lon_grid) to index
hotspot_map = {(row['lat_grid'], row['lon_grid']): idx for idx, row in hotspots.iterrows()}

# === STEP 4: PREPARE FRONTEND RECORDS ===
print("Creating flat records array for frontend...")
df_hotspots = df[df.set_index(['lat_grid', 'lon_grid']).index.isin(hotspots.set_index(['lat_grid', 'lon_grid']).index)].copy()

vehicle_cat_map = {'SCOOTER': 0, 'CAR': 1, 'MOTOR CYCLE': 2, 'PASSENGER AUTO': 3, 'MAXI-CAB': 4, 'OTHERS': 5}
violation_cat_map = {'WRONG PARKING': 0, 'NO PARKING': 1, 'PARKING IN A MAIN ROAD': 2, 'DEFECTIVE NUMBER PLATE': 3, 'PARKING ON FOOTPATH': 4, 'OTHERS': 5}
status_map = {'Approved': 0, 'Rejected': 1, 'Pending Review': 2}

df_hotspots['hotspot_idx'] = df_hotspots.set_index(['lat_grid', 'lon_grid']).index.map(hotspot_map)
df_hotspots['veh_idx'] = df_hotspots['vehicle_cat'].map(vehicle_cat_map)
df_hotspots['viol_idx'] = df_hotspots['violation_cat'].map(violation_cat_map)
df_hotspots['status_idx'] = df_hotspots['status'].map(status_map)

# Group by to create records
grouped_records = df_hotspots.groupby([
    'hotspot_idx', 'hour', 'day_of_week', 'veh_idx', 'viol_idx', 'status_idx'
]).size().reset_index(name='count')

records_list = grouped_records.values.tolist()
print(f"Total flat records: {len(records_list)}")

# === STEP 5: PRE-COMPUTE ENTITY STATISTICS ===
print("Pre-computing Police Station and Junction statistics...")
def aggregate_entity(df_subset, col_name):
    counts = df_subset.groupby(col_name).size().reset_index(name='violations_count')
    daily = df_subset.groupby([col_name, 'date_ist']).size().reset_index(name='daily_count')
    max_daily = daily.groupby(col_name)['daily_count'].max().reset_index(name='max_daily_count')
    counts = counts.merge(max_daily, on=col_name, how='left')
    counts['repeat_offense_rate'] = (counts['violations_count'] - counts['max_daily_count']) / counts['violations_count']
    
    status = df_subset.groupby([col_name, 'status']).size().unstack(fill_value=0).reset_index()
    for s in ['Approved', 'Rejected']:
        if s not in status.columns:
            status[s] = 0
    counts = counts.merge(status, on=col_name, how='left')
    counts['approval_rate'] = counts['Approved'] / (counts['Approved'] + counts['Rejected'])
    counts['approval_rate'] = counts['approval_rate'].fillna(global_avg_approval)
    
    counts_veh = df_subset.groupby([col_name, 'vehicle_cat']).size().reset_index(name='c')
    idx_veh = counts_veh.groupby(col_name)['c'].idxmax()
    dominant_veh = counts_veh.loc[idx_veh, [col_name, 'vehicle_cat']].rename(columns={'vehicle_cat': 'dominant_vehicle'})
    
    counts_viol = df_subset.groupby([col_name, 'violation_cat']).size().reset_index(name='c')
    idx_viol = counts_viol.groupby(col_name)['c'].idxmax()
    dominant_viol = counts_viol.loc[idx_viol, [col_name, 'violation_cat']].rename(columns={'violation_cat': 'dominant_violation'})
    
    counts = counts.merge(dominant_veh, on=col_name, how='left')
    counts = counts.merge(dominant_viol, on=col_name, how='left')
    
    cap = 2000.0 if col_name == 'police_station' else 1000.0
    counts['volume_score'] = np.minimum(100.0, (counts['violations_count'] / cap) * 100.0)
    counts['composite_score'] = (
        0.40 * counts['volume_score'] + 
        0.30 * (counts['approval_rate'] * 100.0) + 
        0.30 * (counts['repeat_offense_rate'] * 100.0)
    )
    return counts.sort_values(by='composite_score', ascending=False).reset_index(drop=True)

ps_stats = aggregate_entity(df, 'police_station')
junc_stats = aggregate_entity(df[df['junction_name'] != 'No Junction'], 'junction_name')

# Format exports
hotspots_export = [{
    'id': int(row['id']),
    'lat': float(row['lat_grid']),
    'lon': float(row['lon_grid']),
    'count': int(row['violations_count']),
    'location': str(row['dominant_location']),
    'junction': str(row['dominant_junction']),
    'police_station': str(row['dominant_police_station']),
    'dominant_vehicle': str(row['dominant_vehicle']),
    'dominant_violation': str(row['dominant_violation']),
    'approval_rate': float(row['approval_rate']),
    'repeat_rate': float(row['repeat_offense_rate']),
    'score': float(row['composite_score'])
} for idx, row in hotspots.iterrows()]

ps_export = [{
    'name': str(row['police_station']),
    'count': int(row['violations_count']),
    'approval_rate': float(row['approval_rate']),
    'repeat_rate': float(row['repeat_offense_rate']),
    'dominant_vehicle': str(row['dominant_vehicle']),
    'dominant_violation': str(row['dominant_violation']),
    'score': float(row['composite_score'])
} for idx, row in ps_stats.iterrows()]

junc_export = [{
    'name': str(row['junction_name']),
    'count': int(row['violations_count']),
    'approval_rate': float(row['approval_rate']),
    'repeat_rate': float(row['repeat_offense_rate']),
    'dominant_vehicle': str(row['dominant_vehicle']),
    'dominant_violation': str(row['dominant_violation']),
    'score': float(row['composite_score'])
} for idx, row in junc_stats.iterrows()]

print(f"Writing to {output_path}...")
js_content = f"""// Pre-processed dataset for Bengaluru Parking Enforcement Intelligence Dashboard
const CATEGORY_MAPS = {{
  hours: {list(range(24))},
  weekdays: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
  vehicles: {json.dumps(list(vehicle_cat_map.keys()))},
  violations: {json.dumps(list(violation_cat_map.keys()))},
  status: {json.dumps(list(status_map.keys()))}
}};

const HOTSPOTS_DATA = {json.dumps(hotspots_export)};
const POLICE_STATIONS_DATA = {json.dumps(ps_export)};
const JUNCTIONS_DATA = {json.dumps(junc_export)};

// Grouped records array for real-time exact filtering in browser:
// Each record is: [hotspot_idx, hour, day_of_week, vehicle_cat_idx, violation_cat_idx, status_idx, count]
const FILTER_RECORDS = {json.dumps(records_list)};

const SYSTEM_INFO = {{
  generated_at: "{pd.Timestamp.now().isoformat()}",
  total_raw_violations: {len(df)},
  global_avg_approval: {global_avg_approval},
  lat_bounds: [{lat_min}, {lat_max}],
  lon_bounds: [{lon_min}, {lon_max}]
}};
"""

with open(output_path, 'w', encoding='utf-8') as f:
    f.write(js_content)

print(f"Data generation complete! Saved in {time.time() - t_start:.2f}s.")
print(f"Output file size: {os.path.getsize(output_path)/1024/1024:.2f} MB")
