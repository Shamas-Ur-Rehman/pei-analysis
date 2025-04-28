import ee
import json
import logging
import os
import geopandas as gpd
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)  # Enable CORS for all routes

# Configure paths
BASE_DIR = Path(__file__).parent
GEOJSON_PATH = Path(r"C:\Users\dell\Desktop\coastal-erosion\api\pei2json.json")
CREDENTIALS_PATH = Path(r"C:\Users\dell\Desktop\ee-api\ee-mohammadmudassir531-747311c353f6.json")
SIMPLIFY_TOLERANCE = 0.001

# Earth Engine initialization
try:
    credentials = ee.ServiceAccountCredentials(
        "gee-4-9@ee-mohammadmudassir531.iam.gserviceaccount.com",
        str(CREDENTIALS_PATH)
    )
    ee.Initialize(credentials)
    logger.info("Earth Engine initialized successfully")
except Exception as e:
    logger.error("Earth Engine initialization failed: %s", str(e))
    raise

# Global geo data
baseline_geometry = None
baseline_land_area = None
processed_geojson = None

def initialize_geodata():
    global baseline_geometry, baseline_land_area, processed_geojson
    try:
        logger.info(f"Loading GeoJSON from: {GEOJSON_PATH}")
        
        if not GEOJSON_PATH.exists():
            raise FileNotFoundError(f"GeoJSON file not found at {GEOJSON_PATH}")

        gdf = gpd.read_file(GEOJSON_PATH)
        
        if gdf.crs != "EPSG:4326":
            logger.info(f"Converting CRS from {gdf.crs} to EPSG:4326")
            gdf = gdf.to_crs("EPSG:4326")
            
        gdf.geometry = gdf.geometry.buffer(0).simplify(SIMPLIFY_TOLERANCE, preserve_topology=True)
        
        union_geom = gdf.geometry.unary_union
        baseline_geometry = ee.Geometry(union_geom.__geo_interface__)
        
        baseline_land_area = baseline_geometry.area(1, ee.Projection('EPSG:32620')).divide(1e6).getInfo()
        logger.info(f"Baseline land area calculated: {baseline_land_area:.2f} km²")
        
        processed_geojson = json.loads(gdf.to_json())
        
    except Exception as e:
        logger.error("GeoJSON initialization failed: %s", str(e))
        raise

try:
    initialize_geodata()
except Exception as e:
    logger.critical("Application initialization failed: %s", str(e))
    raise

@app.route('/')
def serve_index():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/get_baseline')
def get_baseline():
    try:
        return jsonify({
            'geojson': processed_geojson,
            'baseline_area_km2': baseline_land_area
        })
    except Exception as e:
        logger.error("Baseline request failed: %s", str(e))
        return jsonify({'error': str(e)}), 500

@app.route('/get_imagery', methods=['POST'])
def get_imagery():
    try:
        data = request.get_json()
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        
        if not start_date or not end_date:
            raise ValueError("Both start_date and end_date are required")
            
        date_diff = ee.Date(end_date).difference(ee.Date(start_date), 'day').getInfo()
        if date_diff > 365:
            raise ValueError("Date range exceeds 365 day limit")
            
        collection = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                      .filterBounds(baseline_geometry)
                      .filterDate(start_date, end_date)
                      .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20)))
        
        if collection.size().getInfo() == 0:
            raise ValueError("No cloud-free images available for selected dates")
            
        image = collection.median().clip(baseline_geometry)
        ndwi = image.normalizedDifference(['B3', 'B8']).rename('ndwi')
        
        return jsonify({
            'true_color': image.visualize(**{
                'bands': ['B4', 'B3', 'B2'],
                'min': 500,
                'max': 2500,
                'gamma': 1.2
            }).getMapId()['tile_fetcher'].url_format,
            'ndwi': ndwi.visualize(**{
                'min': -0.5,
                'max': 0.8,
                'palette': ['red', 'yellow', 'green', 'blue']
            }).getMapId()['tile_fetcher'].url_format
        })
        
    except Exception as e:
        logger.error("Imagery request failed: %s", str(e))
        return jsonify({'error': str(e)}), 500

@app.route('/analyze_change', methods=['POST'])
def analyze_change():
    try:
        data = request.get_json()
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        
        if not start_date or not end_date:
            raise ValueError("Both start_date and end_date are required")

        collection = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                      .filterBounds(baseline_geometry)
                      .filterDate(start_date, end_date)
                      .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20)))
        
        if collection.size().getInfo() == 0:
            raise ValueError(f"No Sentinel-2 images between {start_date} and {end_date}")
            
        median_image = collection.median()
        ndwi = median_image.normalizedDifference(['B3', 'B8'])
        land_mask = ndwi.lt(0.2).rename('land_mask')

        current_area = land_mask.multiply(ee.Image.pixelArea()) \
            .reduceRegion(
                reducer=ee.Reducer.sum(),
                geometry=baseline_geometry,
                scale=10,
                maxPixels=1e13
            ).get('land_mask').getInfo() or 0
        
        current_area_km2 = round(current_area / 1e6, 2)
        erosion_area = round(baseline_land_area - current_area_km2, 2)

        erosion_vis = land_mask.Not().selfMask().visualize(
            **{'palette': ['red'], 'min': 1, 'max': 1}
        )

        return jsonify({
            'baseline_land_area': round(baseline_land_area, 2),
            'current_land_area': current_area_km2,
            'erosion_area': erosion_area,
            'erosion_tiles': erosion_vis.getMapId()['tile_fetcher'].url_format
        })
        
    except Exception as e:
        logger.error("Analysis failed: %s", str(e))
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)