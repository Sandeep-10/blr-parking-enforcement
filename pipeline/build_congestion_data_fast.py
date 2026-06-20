import pandas as pd
import numpy as np
import json
import os
import time
import math
import sys
import requests

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

# Overpass API endpoints for rotation/failover
overpass_endpoints = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter"
]
endpoint_idx = 0

headers = {
    'User-Agent': 'BengaluruTrafficCongestionModel/1.0 (agentic-coder-flipkart-hackathon; contact: chang@users.noreply.github.com)'
}

def query_overpass_failover(query_str):
    global endpoint_idx
    retries_per_server = 2
    
    for attempt in range(len(overpass_endpoints) * retries_per_server):
        url = overpass_endpoints[endpoint_idx]
        try:
            # print(f"    Trying Overpass endpoint: {url}")
            res = requests.post(url, data={'data': query_str}, headers=headers, timeout=60)
            if res.status_code == 200:
                return res.json()
            elif res.status_code == 429:
                print(f"    Rate limited (429) on {url}. Switching endpoint...")
            else:
                print(f"    HTTP {res.status_code} on {url}. Switching endpoint...")
        except Exception as ex:
            print(f"    Request failed on {url}: {ex}. Switching endpoint...")
            
        # Rotate to next endpoint
        endpoint_idx = (endpoint_idx + 1) % len(overpass_endpoints)
        time.sleep(2)
        
    raise Exception("All Overpass endpoints failed or rate-limited!")

def get_dominant_meta(df_subset, group_cols, target_col, name):
    counts = df_subset.groupby(group_cols + [target_col]).size().reset_index(name='c')
    idx = counts.groupby(group_cols)['c'].idxmax()
    return counts.loc[idx, group_cols + [target_col]].rename(columns={target_col: name})

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

# Normalization functions
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
print(f"Total hotspots (count >= 5): {len(hotspots)}")

# Calculate Repeat Offense and Approval Rates
hotspot_daily = df.groupby(['lat_grid', 'lon_grid', 'date_ist']).size().reset_index(name='daily_count')
hotspot_max_daily = hotspot_daily.groupby(['lat_grid', 'lon_grid'])['daily_count'].max().reset_index(name='max_daily_count')
hotspots = hotspots.merge(hotspot_max_daily, on=['lat_grid', 'lon_grid'], how='left')
hotspots['repeat_offense_rate'] = (hotspots['violations_count'] - hotspots['max_daily_count']) / hotspots['violations_count']

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

# Find dominant metadata
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

# Add hotspot ID map
hotspots = hotspots.reset_index(drop=True)
hotspots['id'] = hotspots.index
hotspot_map = {(row['lat_grid'], row['lon_grid']): idx for idx, row in hotspots.iterrows()}

# ==========================================================
# STEP 1: SOURCE THE ROAD-NETWORK DATA & POIS
# ==========================================================
# 1A. Fetch and Cache POIs
pois = []
if os.path.exists(pois_cache_path):
    print("Loading POIs from cache...")
    with open(pois_cache_path, 'r', encoding='utf-8') as f:
        pois = json.load(f)
else:
    print("Querying Overpass for POIs...")
    bbox = f"{lat_min},{lon_min},{lat_max},{lon_max}"
    overpass_query = f"""
    [out:json][timeout:90];
    (
      node[railway=station]({bbox});
      way[railway=station]({bbox});
      node[station=subway]({bbox});
      node[highway=bus_stop]({bbox});
      node[amenity=bus_station]({bbox});
      node[shop=mall]({bbox});
      way[shop=mall]({bbox});
      node[amenity=market]({bbox});
      node[shop=supermarket]({bbox});
    );
    out center;
    """
    try:
        res = query_overpass_failover(overpass_query)
        pois_data = res.get('elements', [])
        for e in pois_data:
            tags = e.get('tags', {})
            name = tags.get('name', 'Unnamed POI')
            lat = e.get('lat', e.get('center', {}).get('lat'))
            lon = e.get('lon', e.get('center', {}).get('lon'))
            
            # Determine type
            poi_type = 'other'
            if tags.get('railway') == 'station' or tags.get('station') == 'subway':
                poi_type = 'metro_station'
            elif tags.get('highway') == 'bus_stop':
                poi_type = 'bus_stop'
            elif tags.get('amenity') == 'bus_station':
                poi_type = 'bus_station'
            elif tags.get('shop') == 'mall' or tags.get('amenity') == 'market':
                poi_type = 'mall_market'
            elif tags.get('shop') == 'supermarket':
                poi_type = 'supermarket'
            
            if lat and lon:
                pois.append({
                    'name': name,
                    'lat': float(lat),
                    'lon': float(lon),
                    'type': poi_type
                })
        with open(pois_cache_path, 'w', encoding='utf-8') as f:
            json.dump(pois, f)
        print(f"Sourced {len(pois)} POIs and saved to cache.")
    except Exception as ex:
        print(f"Failed POI request: {ex}")

# 1B. Deduplicate hotspot coordinates and fetch roads around them
ways = []
if os.path.exists(ways_cache_path):
    print("Loading Road Segments from cache...")
    with open(ways_cache_path, 'r', encoding='utf-8') as f:
        ways = json.load(f)
else:
    # Deduplicate coordinates by rounding to 3 decimals (~111m)
    hotspots['lat_3d'] = np.round(hotspots['lat_grid'], 3)
    hotspots['lon_3d'] = np.round(hotspots['lon_grid'], 3)
    unique_coords = hotspots.groupby(['lat_3d', 'lon_3d']).size().reset_index()
    coords_list = unique_coords[['lat_3d', 'lon_3d']].values.tolist()
    
    print(f"Deduplicated 6,279 hotspots into {len(coords_list)} query centroids.")
    print("Querying Overpass for Roads near centroids in batches of 40 (around:120m)...")
    
    batch_size = 40
    for start_idx in range(0, len(coords_list), batch_size):
        end_idx = min(start_idx + batch_size, len(coords_list))
        batch = coords_list[start_idx:end_idx]
        
        print(f"  Batch {start_idx // batch_size + 1} / {math.ceil(len(coords_list) / batch_size)} (indices {start_idx} to {end_idx})...")
        
        union_lines = []
        for lat, lon in batch:
            union_lines.append(f'  way(around:120, {lat}, {lon})["highway"~"primary|secondary|tertiary|residential|trunk|service|unclassified"];')
        
        union_query_str = "\n".join(union_lines)
        overpass_query = f"""
        [out:json][timeout:90];
        (
        {union_query_str}
        );
        out geom;
        """
        
        try:
            res = query_overpass_failover(overpass_query)
            elements = res.get('elements', [])
            for e in elements:
                if e.get('type') == 'way' and 'geometry' in e:
                    ways.append({
                        'id': e['id'],
                        'tags': e.get('tags', {}),
                        'geometry': e['geometry']
                    })
            print(f"    Returned {len(elements)} elements. Total roads so far: {len(ways)}")
        except Exception as ex:
            print(f"    Batch failed: {ex}. Skipping batch to continue pipeline...")
        
        # Friendly rate limiting delay
        time.sleep(1.5)
        
    # De-duplicate ways by ID
    unique_ways = {}
    for w in ways:
        unique_ways[w['id']] = w
    ways = list(unique_ways.values())
    
    with open(ways_cache_path, 'w', encoding='utf-8') as f:
        json.dump(ways, f)
    print(f"Completed road segments sourcing. Total unique segments: {len(ways)}")

# ==========================================================
# STEP 2: SNAPPING & ROAD ATTRIBUTE ASSOCIATION
# ==========================================================
print("Snapping hotspots to nearest road segments using a spatial grid index...")

# Coordinate distance helpers
def point_to_segment_distance(px, py, ax, ay, bx, by):
    abx = bx - ax
    aby = by - ay
    apx = px - ax
    apy = py - ay
    ab_len_sq = abx*abx + aby*aby
    if ab_len_sq == 0:
        return math.sqrt((px-ax)**2 + (py-ay)**2)
    t = (apx*abx + apy*aby) / ab_len_sq
    t = max(0.0, min(1.0, t))
    cx = ax + t * abx
    cy = ay + t * aby
    return math.sqrt((px-cx)**2 + (py-cy)**2)

# Approximate lat/lon projection to meters around Bengaluru
lat_avg = 12.97
m_per_deg_lat = 111132.9
m_per_deg_lon = 111132.9 * math.cos(math.radians(lat_avg))

# Build spatial grid index
grid_ways = {}
grid_size_index = 0.005 # ~550m

for w in ways:
    geom = w['geometry']
    lats = [pt['lat'] for pt in geom]
    lons = [pt['lon'] for pt in geom]
    
    # Bounding box of the way
    lat_min_w, lat_max_w = min(lats), max(lats)
    lon_min_w, lon_max_w = min(lons), max(lons)
    
    # Cells this bounding box overlaps
    cell_lat_start = int(math.floor(lat_min_w / grid_size_index))
    cell_lat_end = int(math.ceil(lat_max_w / grid_size_index))
    cell_lon_start = int(math.floor(lon_min_w / grid_size_index))
    cell_lon_end = int(math.ceil(lon_max_w / grid_size_index))
    
    for clat in range(cell_lat_start, cell_lat_end + 1):
        for clon in range(cell_lon_start, cell_lon_end + 1):
            cell_key = (clat, clon)
            if cell_key not in grid_ways:
                grid_ways[cell_key] = []
            grid_ways[cell_key].append(w)

def snap_hotspot_to_road_indexed(lat, lon):
    px = lon * m_per_deg_lon
    py = lat * m_per_deg_lat
    
    clat = int(round(lat / grid_size_index))
    clon = int(round(lon / grid_size_index))
    
    # Check 3x3 surrounding grid cells
    candidate_ways = []
    seen_ids = set()
    for dlat in [-1, 0, 1]:
        for dlon in [-1, 0, 1]:
            key = (clat + dlat, clon + dlon)
            if key in grid_ways:
                for w in grid_ways[key]:
                    if w['id'] not in seen_ids:
                        seen_ids.add(w['id'])
                        candidate_ways.append(w)
                        
    if not candidate_ways:
        return None, 999.0
        
    min_dist = float('inf')
    best_way = None
    
    for w in candidate_ways:
        geom = w['geometry']
        for idx in range(len(geom) - 1):
            pt_a = geom[idx]
            pt_b = geom[idx + 1]
            ax = pt_a['lon'] * m_per_deg_lon
            ay = pt_a['lat'] * m_per_deg_lat
            bx = pt_b['lon'] * m_per_deg_lon
            by = pt_b['lat'] * m_per_deg_lat
            
            d = point_to_segment_distance(px, py, ax, ay, bx, by)
            if d < min_dist:
                min_dist = d
                best_way = w
                
    return best_way, min_dist

# Standard fallbacks for missing attributes
highway_type_capacity = {
    'trunk': 5.0,
    'trunk_link': 5.0,
    'primary': 4.0,
    'primary_link': 4.0,
    'secondary': 3.0,
    'secondary_link': 3.0,
    'tertiary': 2.0,
    'tertiary_link': 2.0,
    'residential': 1.0,
    'living_street': 1.0,
    'service': 0.8,
    'unclassified': 1.0
}

fallback_lanes = {
    'trunk': 3,
    'primary': 3,
    'secondary': 2,
    'tertiary': 1.5,
    'residential': 1.0,
    'service': 1.0,
    'unclassified': 1.0
}

fallback_maxspeed = {
    'trunk': 60,
    'primary': 50,
    'secondary': 40,
    'tertiary': 30,
    'residential': 20,
    'service': 20,
    'unclassified': 20
}

snapped_results = []
missing_highway_count = 0
missing_lanes_count = 0
missing_speed_count = 0

for idx, row in hotspots.iterrows():
    lat = row['lat_grid']
    lon = row['lon_grid']
    
    best_way, dist = snap_hotspot_to_road_indexed(lat, lon)
    
    # Snap threshold is 120m.
    if best_way is None or dist > 120:
        h_type = 'unclassified'
        lanes = 1.0
        maxspeed = 20
        road_name = 'Unknown Road (Untagged)'
        missing_highway_count += 1
    else:
        tags = best_way['tags']
        road_name = tags.get('name', 'Unnamed Street')
        h_type = tags.get('highway', 'unclassified')
        
        # Lanes parsing
        lanes_tag = tags.get('lanes')
        if lanes_tag:
            try:
                if ';' in str(lanes_tag):
                    lanes = float(str(lanes_tag).split(';')[0])
                else:
                    lanes = float(lanes_tag)
            except ValueError:
                lanes = fallback_lanes.get(h_type, 1.0)
                missing_lanes_count += 1
        else:
            lanes = fallback_lanes.get(h_type, 1.0)
            missing_lanes_count += 1
            
        # Maxspeed parsing
        speed_tag = tags.get('maxspeed')
        if speed_tag:
            try:
                speed_str = str(speed_tag).lower().replace('km/h', '').replace('mph', '').strip()
                maxspeed = float(speed_str)
            except ValueError:
                maxspeed = fallback_maxspeed.get(h_type, 20)
                missing_speed_count += 1
        else:
            maxspeed = fallback_maxspeed.get(h_type, 20)
            missing_speed_count += 1
            
    snapped_results.append({
        'road_name': road_name,
        'highway_type': h_type,
        'lanes': lanes,
        'maxspeed': maxspeed,
        'snap_dist_meters': dist
    })

snapped_df = pd.DataFrame(snapped_results)
hotspots = pd.concat([hotspots, snapped_df], axis=1)

print(f"OSM tag coverage review:")
print(f"  - Hotspots snapped beyond 120m (or untagged): {missing_highway_count} ({missing_highway_count/len(hotspots)*100:.2f}%)")
print(f"  - Hotspots using fallback lane count: {missing_lanes_count} ({missing_lanes_count/len(hotspots)*100:.2f}%)")
print(f"  - Hotspots using fallback max speed: {missing_speed_count} ({missing_speed_count/len(hotspots)*100:.2f}%)")

# ==========================================================
# STEP 3: PEAK WINDOWS AND POI PROXIMITY CALCULATIONS
# ==========================================================
print("Calculating observed peak hour overlap (8-11 AM and 5-8 PM)...")
peak_hours = [8, 9, 10, 17, 18, 19] # 8:00 - 10:59, 17:00 - 19:59 IST
df['is_peak'] = df['hour'].isin(peak_hours)

peak_violations = df[df['is_peak']].groupby(['lat_grid', 'lon_grid']).size().reset_index(name='peak_violations_count')
hotspots = hotspots.merge(peak_violations, on=['lat_grid', 'lon_grid'], how='left')
hotspots['peak_violations_count'] = hotspots['peak_violations_count'].fillna(0)
hotspots['peak_hour_overlap'] = hotspots['peak_violations_count'] / hotspots['violations_count']

# Compute distance-decay POI proximity score within 300m
print("Calculating proximity-weighted POI counts...")
poi_multipliers = {
    'metro_station': 3.0,
    'bus_station': 2.0,
    'mall_market': 2.0,
    'bus_stop': 1.0,
    'supermarket': 1.0,
    'other': 1.0
}

poi_proximity_scores = []
poi_raw_counts = []

for idx, row in hotspots.iterrows():
    lat = row['lat_grid']
    lon = row['lon_grid']
    
    score_sum = 0.0
    count_300m = 0
    
    for p in pois:
        if abs(p['lat'] - lat) <= 0.003 and abs(p['lon'] - lon) <= 0.003:
            lat_rad = math.radians((lat + p['lat']) / 2.0)
            dy = math.radians(p['lat'] - lat) * 6371000.0
            dx = math.radians(p['lon'] - lon) * math.cos(lat_rad) * 6371000.0
            dist = math.sqrt(dx*dx + dy*dy)
            
            if dist <= 300.0:
                count_300m += 1
                decay_weight = 1.0 / (dist / 100.0 + 1.0)
                mult = poi_multipliers.get(p['type'], 1.0)
                score_sum += mult * decay_weight
                
    poi_proximity_scores.append(score_sum)
    poi_raw_counts.append(count_300m)

hotspots['poi_proximity_raw'] = poi_proximity_scores
hotspots['poi_count_300m'] = poi_raw_counts

# ==========================================================
# STEP 4: COMPOSITE CONGESTION IMPACT SCORE CALCULATION
# ==========================================================
print("Engineering Congestion-Impact Score components...")

# 4A. Violation density (normalized to 0-1)
max_density = hotspots['violations_count'].max()
hotspots['violation_density'] = hotspots['violations_count'] / max_density

# 4B. Narrowness penalty (derived from highway capacity ordinal * lanes)
hotspots['highway_score'] = hotspots['highway_type'].map(highway_type_capacity).fillna(1.0)
hotspots['capacity_proxy'] = hotspots['highway_score'] * hotspots['lanes']

# Penalty is 1 / capacity. We scale it to [0.2, 1.0] so wide segments don't hit 0 multiplier
raw_penalty = 1.0 / hotspots['capacity_proxy']
min_pen, max_pen = raw_penalty.min(), raw_penalty.max()
hotspots['narrowness_penalty'] = 0.2 + 0.8 * ((raw_penalty - min_pen) / (max_pen - min_pen))

# 4C. POI proximity score (normalized to 0-1)
max_poi_score = hotspots['poi_proximity_raw'].max() if hotspots['poi_proximity_raw'].max() > 0 else 1.0
hotspots['poi_proximity_score'] = hotspots['poi_proximity_raw'] / max_poi_score

# 4D. Calculate formula: Density * Narrowness * (1 + PeakOverlap) * (1 + POIProximity)
hotspots['congestion_impact_raw'] = (
    hotspots['violation_density'] * 
    hotspots['narrowness_penalty'] * 
    (1.0 + hotspots['peak_hour_overlap']) * 
    (1.0 + hotspots['poi_proximity_score'])
)

# Normalize Congestion Impact Score to 0-100 scale
max_impact = hotspots['congestion_impact_raw'].max()
hotspots['congestion_impact_score'] = (hotspots['congestion_impact_raw'] / max_impact) * 100.0

# Day 1 score calculations
hotspots['volume_score'] = np.minimum(100.0, (hotspots['violations_count'] / 500.0) * 100.0)
hotspots['approval_score'] = hotspots['approval_rate'] * 100.0
hotspots['repeat_score'] = hotspots['repeat_offense_rate'] * 100.0
hotspots['day1_score'] = (
    0.40 * hotspots['volume_score'] + 
    0.30 * hotspots['approval_score'] + 
    0.30 * hotspots['repeat_score']
)

# Sort hotspots by Congestion Impact Score descending
hotspots = hotspots.sort_values(by='congestion_impact_score', ascending=False).reset_index(drop=True)
hotspots['id'] = hotspots.index
hotspot_map = {(row['lat_grid'], row['lon_grid']): idx for idx, row in hotspots.iterrows()}

print("\n--- SANITY CHECK: REORDERING COMPARISONS ---")
print("Top 10 Hotspots by Congestion Impact Score:")
for idx, row in hotspots.head(10).iterrows():
    print(f"Rank {idx+1}: {row['dominant_location'].split(',')[0]} (Score: {row['congestion_impact_score']:.1f})")
    print(f"  - Vol: {row['violations_count']} (Density norm: {row['violation_density']:.2f})")
    print(f"  - Road: {row['road_name']} ({row['highway_type']}, {row['lanes']} lanes)")
    print(f"  - Narrowness Penalty norm: {row['narrowness_penalty']:.2f}")
    print(f"  - Peak Hour Overlap: {row['peak_hour_overlap']:.1%}")
    print(f"  - POIs in 300m: {row['poi_count_300m']} (Score norm: {row['poi_proximity_score']:.2f})")
    print("-" * 50)

# Merge back vol rank to see swaps
hotspots_by_vol = hotspots.sort_values(by='violations_count', ascending=False).reset_index()
hotspots_by_vol['vol_rank'] = hotspots_by_vol.index + 1
hotspots = hotspots.merge(hotspots_by_vol[['id', 'vol_rank']], on='id')
hotspots['impact_rank'] = hotspots.index + 1
hotspots['rank_delta'] = hotspots['vol_rank'] - hotspots['impact_rank']

print("\nRank Swaps (Volume Rank vs. Congestion Impact Rank):")
swaps_up = hotspots[hotspots['rank_delta'] > 500].sort_values(by='congestion_impact_score', ascending=False).head(3)
for idx, row in swaps_up.iterrows():
    print(f"PROMOTED: {row['dominant_location'].split(',')[0]}")
    print(f"  - Vol Rank: {row['vol_rank']} (Vol: {row['violations_count']}) -> Congestion Rank: {row['impact_rank']} (Score: {row['congestion_impact_score']:.1f})")
    print(f"  - Why: {row['road_name']} ({row['highway_type']}, {row['lanes']} lanes), Peak overlap: {row['peak_hour_overlap']:.1%}, POIs: {row['poi_count_300m']}")

swaps_down = hotspots[hotspots['rank_delta'] < -500].sort_values(by='violations_count', ascending=False).head(3)
for idx, row in swaps_down.iterrows():
    print(f"DEMOTED: {row['dominant_location'].split(',')[0]}")
    print(f"  - Vol Rank: {row['vol_rank']} (Vol: {row['violations_count']}) -> Congestion Rank: {row['impact_rank']} (Score: {row['congestion_impact_score']:.1f})")
    print(f"  - Why: {row['road_name']} ({row['highway_type']}, {row['lanes']} lanes), Peak overlap: {row['peak_hour_overlap']:.1%}, POIs: {row['poi_count_300m']}")

# ==========================================================
# STEP 5: PREPARE FRONTEND RECORDS & ENTITY STATISTICS
# ==========================================================
print("Creating flat records array for frontend...")
df_hotspots = df[df.set_index(['lat_grid', 'lon_grid']).index.isin(hotspots.set_index(['lat_grid', 'lon_grid']).index)].copy()

vehicle_cat_map = {'SCOOTER': 0, 'CAR': 1, 'MOTOR CYCLE': 2, 'PASSENGER AUTO': 3, 'MAXI-CAB': 4, 'OTHERS': 5}
violation_cat_map = {'WRONG PARKING': 0, 'NO PARKING': 1, 'PARKING IN A MAIN ROAD': 2, 'DEFECTIVE NUMBER PLATE': 3, 'PARKING ON FOOTPATH': 4, 'OTHERS': 5}
status_map = {'Approved': 0, 'Rejected': 1, 'Pending Review': 2}

df_hotspots['hotspot_idx'] = df_hotspots.set_index(['lat_grid', 'lon_grid']).index.map(hotspot_map)
df_hotspots['veh_idx'] = df_hotspots['vehicle_cat'].map(vehicle_cat_map)
df_hotspots['viol_idx'] = df_hotspots['violation_cat'].map(violation_cat_map)
df_hotspots['status_idx'] = df_hotspots['status'].map(status_map)

grouped_records = df_hotspots.groupby([
    'hotspot_idx', 'hour', 'day_of_week', 'veh_idx', 'viol_idx', 'status_idx'
]).size().reset_index(name='count')
records_list = grouped_records.values.tolist()
print(f"Total flat records: {len(records_list)}")

print("Aggregating Police Stations and Junctions...")
def aggregate_entity(df_subset, col_name, hostspot_df):
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
    
    entity_col = 'dominant_police_station' if col_name == 'police_station' else 'dominant_junction'
    entity_congestion = hostspot_df.groupby(entity_col)['congestion_impact_score'].mean().reset_index()
    entity_congestion.columns = [col_name, 'congestion_score']
    counts = counts.merge(entity_congestion, on=col_name, how='left')
    counts['congestion_score'] = counts['congestion_score'].fillna(0)
    
    cap = 2000.0 if col_name == 'police_station' else 1000.0
    counts['volume_score'] = np.minimum(100.0, (counts['violations_count'] / cap) * 100.0)
    counts['day1_score'] = (
        0.40 * counts['volume_score'] + 
        0.30 * (counts['approval_rate'] * 100.0) + 
        0.30 * (counts['repeat_offense_rate'] * 100.0)
    )
    return counts

ps_stats = aggregate_entity(df, 'police_station', hotspots)
junc_stats = aggregate_entity(df[df['junction_name'] != 'No Junction'], 'junction_name', hotspots)

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
    'day1_score': float(row['day1_score']),
    
    'road_name': str(row['road_name']),
    'highway_type': str(row['highway_type']),
    'lanes': float(row['lanes']),
    'maxspeed': float(row['maxspeed']),
    'peak_overlap': float(row['peak_hour_overlap']),
    'poi_count': int(row['poi_count_300m']),
    'poi_score_norm': float(row['poi_proximity_score']),
    'narrowness_norm': float(row['narrowness_penalty']),
    'congestion_score': float(row['congestion_impact_score'])
} for idx, row in hotspots.iterrows()]

ps_export = [{
    'name': str(row['police_station']),
    'count': int(row['violations_count']),
    'approval_rate': float(row['approval_rate']),
    'repeat_rate': float(row['repeat_offense_rate']),
    'dominant_vehicle': str(row['dominant_vehicle']),
    'dominant_violation': str(row['dominant_violation']),
    'day1_score': float(row['day1_score']),
    'congestion_score': float(row['congestion_score'])
} for idx, row in ps_stats.sort_values(by='congestion_score', ascending=False).iterrows()]

junc_export = [{
    'name': str(row['junction_name']),
    'count': int(row['violations_count']),
    'approval_rate': float(row['approval_rate']),
    'repeat_rate': float(row['repeat_offense_rate']),
    'dominant_vehicle': str(row['dominant_vehicle']),
    'dominant_violation': str(row['dominant_violation']),
    'day1_score': float(row['day1_score']),
    'congestion_score': float(row['congestion_score'])
} for idx, row in junc_stats.sort_values(by='congestion_score', ascending=False).iterrows()]

# Generate JS database
js_content = f"""// Pre-processed dataset for Bengaluru Parking Enforcement Intelligence Dashboard with Congestion-Impact Scoring Layer
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

# Write to both locations
print(f"Writing database to {output_js_path} ...")
with open(output_js_path, 'w', encoding='utf-8') as f:
    f.write(js_content)

print(f"Writing database to {dist_js_path} ...")
os.makedirs(os.path.dirname(dist_js_path), exist_ok=True)
with open(dist_js_path, 'w', encoding='utf-8') as f:
    f.write(js_content)

print(f"Database generation complete! Saved in {time.time() - t_start:.2f}s.")
print(f"Output file size: {os.path.getsize(output_js_path)/1024/1024:.2f} MB")
