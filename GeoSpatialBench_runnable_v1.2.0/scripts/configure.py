"""Create an editable configuration for common four-layer GeoPackages."""
import os
from pathlib import Path

from io_utils import write_json

ALIASES = {'pois':['pois','poi','points','gis_osm_pois_free'],
           'roads':['roads','road','gis_osm_roads_free'],
           'buildings':['buildings','building','gis_osm_buildings_a_free'],
           'landuse':['landuse','land_use','regions','gis_osm_landuse_a_free']}

def init_config(data, city, prefix, path, target_crs=None):
    import geopandas as gpd
    import pyogrio
    from pyproj import CRS
    data,path = Path(data).resolve(),Path(path).resolve()
    if path.exists():
        raise ValueError(f'Configuration already exists: {path}')
    layers = {str(name).lower():str(name) for name,_ in pyogrio.list_layers(data)}
    config = {'city_name':city,'id_prefix':prefix,'dataset_name':'Describe your vector source and release date',
              'target_crs':target_crs,'output_dir':'../outputs/'+prefix.lower(),'seed':20260819,'inputs':{},
              'filters':{'road_length_m':[30,1200],'building_area_m2':[50,30000],'landuse_area_m2':[20000,3000000]}}
    for role,aliases in ALIASES.items():
        matches = [layers[name] for name in aliases if name in layers]
        if len(matches)!=1:
            raise ValueError(f'Cannot unambiguously infer {role} layer; use inspect and edit config/city.example.json. Candidates: {matches}')
        info = pyogrio.read_info(data,layer=matches[0])
        fields = set(info['fields'])
        entry = {'path':os.path.relpath(data,path.parent).replace('\\','/'),'layer':matches[0],
                 'attribute_fields':[f for f in ('name','fclass','type') if f in fields]}
        ids = [f for f in ('source_id','osm_id','osm_id2','id') if f in fields]
        if ids:
            entry['id_field'] = ids[0]
        if role=='roads':
            layer_field = next((f for f in ('osm_layer','layer','z_order') if f in fields),None)
            if layer_field is None:
                raise ValueError('Roads lack a numeric ordinal layer field for O06; add it from verified source attributes and configure field_map.layer')
            entry['field_map'] = {'layer':layer_field}
            entry['field_map'].update({f:f for f in ('bridge','tunnel') if f in fields})
        config['inputs'][role] = entry
    if target_crs is None:
        pois = gpd.read_file(data,layer=config['inputs']['pois']['layer'],rows=1000)
        if pois.crs is None:
            raise ValueError('Input lacks CRS; set source_crs in a manual configuration')
        source = CRS(pois.crs)
        if source.is_projected and all(axis.unit_name.lower() in ('metre','meter') for axis in source.axis_info):
            target_crs = source.to_string()
        else:
            target_crs = pois.estimate_utm_crs().to_string()
        config['target_crs'] = target_crs
    write_json(path,config)
    return {'config':str(path),'target_crs':target_crs,'status':'CREATED',
            'next_step':'Review the inferred layers, CRS, source description and filter thresholds; then run check'}
