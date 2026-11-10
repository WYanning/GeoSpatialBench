"""Deterministic offline maps in the task's metric frame; no tile requests."""
import json
import math
from pathlib import Path

from io_utils import read_rows, write_json, sha256
from prompts import choose

SIZE = 1536
PLOT = (100, 150, 1436, 1400)  # left, top, right, bottom in PNG pixels
COLORS = ['#2066a8','#de6835','#238c6b','#9b59b6','#bd8b1e','#536678']

def scene_hash(row, editing):
    """Hash visible initial geometry and attributes, excluding reference edits."""
    import hashlib
    features = scene_features(row,editing)
    visible = {'id':row['task_id' if editing else 'item_id'],'relation_id':row['relation_id'],
               'features':[(fid,geometry.wkt,attrs) for fid,geometry,attrs in features]}
    return hashlib.sha256(json.dumps(visible,sort_keys=True,ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()

def image_metadata(image_path, row, editing):
    from PIL import Image
    image_path = Path(image_path)
    metadata_path = image_path.with_suffix('.frame.json')
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    if metadata.get('scene_sha256') != scene_hash(row,editing):
        raise ValueError(f'Image belongs to a different scene: {image_path.name}')
    if metadata.get('image_sha256') != sha256(image_path):
        raise ValueError(f'Image hash differs from render metadata: {image_path.name}')
    expected = frame_for(row,editing)
    if any(metadata.get(key) != value for key,value in expected.items()):
        raise ValueError(f'Image coordinate frame differs from the current renderer: {image_path.name}')
    with Image.open(image_path) as image:
        image.verify()
        if image.size != (SIZE,SIZE) or image.format != 'PNG':
            raise ValueError('Expected a 1536 x 1536 PNG')
    return metadata

def scene_features(task, editing):
    from shapely import wkt
    if editing:
        return [(f['feature_id'],wkt.loads(f['wkt']),f.get('semantics',{})) for f in task['scene_features']]
    return [(f['id'],wkt.loads(f['geometry_wkt']),f.get('source_attributes',{})) for f in task['objects']]

def frame_for(task, editing=True):
    features = scene_features(task, editing)
    minx = min(g.bounds[0] for _,g,_ in features)
    miny = min(g.bounds[1] for _,g,_ in features)
    maxx = max(g.bounds[2] for _,g,_ in features)
    maxy = max(g.bounds[3] for _,g,_ in features)
    left,top,right,bottom = PLOT
    scale = max((maxx-minx)/(right-left),(maxy-miny)/(bottom-top),0.01)*1.25
    cx,cy = (minx+maxx)/2,(miny+maxy)/2
    return {'xmin':cx-(right-left)*scale/2,'ymax':cy+(bottom-top)*scale/2,
            'metres_per_pixel':scale,'plot_pixels':list(PLOT),'canvas_pixels':[SIZE,SIZE],
            'renderer':'offline_metric_v1'}

def to_pixels(geometry, frame):
    from shapely import affinity
    scale = frame['metres_per_pixel']
    left,top,_,_ = PLOT
    return affinity.affine_transform(geometry,[1/scale,0,0,-1/scale,
                                              left-frame['xmin']/scale,top+frame['ymax']/scale])

def from_pixels(geometry, frame):
    from shapely import affinity
    scale = frame['metres_per_pixel']
    left,top,_,_ = PLOT
    return affinity.affine_transform(geometry,[scale,0,0,-scale,
                                              frame['xmin']-left*scale,frame['ymax']+top*scale])

def draw_geometry(ax, geom, color):
    from matplotlib.path import Path as MPath
    from matplotlib.patches import PathPatch
    if hasattr(geom,'geoms'):
        for part in geom.geoms:
            draw_geometry(ax,part,color)
    elif geom.geom_type == 'Point':
        ax.scatter([geom.x],[geom.y],s=75,color=color,edgecolors='white',linewidths=1,zorder=5)
    elif geom.geom_type in ('LineString','LinearRing'):
        xs,ys = geom.xy
        ax.plot(xs,ys,color=color,linewidth=2.6,zorder=3)
        if len(xs)>1:
            ax.annotate('',xy=(xs[-1],ys[-1]),xytext=(xs[-2],ys[-2]),
                        arrowprops={'arrowstyle':'->','color':color,'lw':2},zorder=4)
            ax.scatter([xs[0]],[ys[0]],color=color,s=16,zorder=4)
    elif geom.geom_type == 'Polygon':
        # Preserve holes with consistently oriented rings.
        from shapely.geometry.polygon import orient
        geom = orient(geom,sign=1.0)
        vertices,codes = [],[]
        for ring in [geom.exterior,*geom.interiors]:
            coords = list(ring.coords)
            vertices.extend(coords)
            codes.extend([MPath.MOVETO]+[MPath.LINETO]*(len(coords)-2)+[MPath.CLOSEPOLY])
        ax.add_patch(PathPatch(MPath(vertices,codes),facecolor=color,edgecolor=color,alpha=.24,lw=2))

def render_case(case, task='both', per_relation=0):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['savefig.bbox'] = None
    from matplotlib.lines import Line2D
    case = Path(case).resolve()
    counts = {}
    for kind in ('qa','editing'):
        if task not in ('both',kind):
            continue
        editing = kind == 'editing'
        rows = choose(read_rows(case/kind/('tasks.jsonl' if editing else 'items.jsonl')),per_relation)
        target = case/'images'/kind
        target.mkdir(parents=True,exist_ok=True)
        for row in rows:
            row_id = row['task_id' if editing else 'item_id']
            frame = frame_for(row,editing)
            image_path = target/f'{row_id}.png'
            if image_path.exists():
                image_metadata(image_path,row,editing)
                continue
            features = scene_features(row,editing)
            fig = plt.figure(figsize=(12,12),dpi=128,facecolor='#ffffff')
            left,top,right,bottom = PLOT
            ax = fig.add_axes([left/SIZE,(SIZE-bottom)/SIZE,(right-left)/SIZE,(bottom-top)/SIZE])
            mpp = frame['metres_per_pixel']
            ax.set_xlim(frame['xmin'],frame['xmin']+(right-left)*mpp)
            ax.set_ylim(frame['ymax']-(bottom-top)*mpp,frame['ymax'])
            ax.set_aspect('equal')
            ax.set_facecolor('#f6f8fa')
            handles = []
            for i,(fid,geom,attrs) in enumerate(features):
                color = COLORS[i%len(COLORS)]
                draw_geometry(ax,geom,color)
                c = geom.centroid
                ax.annotate(fid,(c.x,c.y),xytext=(5+8*(i%3),5+10*(i%4)),textcoords='offset points',
                            color=color,fontsize=10,weight='bold',zorder=8,
                            bbox={'facecolor':'white','alpha':.8,'edgecolor':'none','pad':1.2})
                label = fid
                if row['relation_id']=='O06':
                    label += f" (layer={attrs.get('layer',attrs.get('osm_layer','?'))})"
                handles.append(Line2D([0],[0],color=color,lw=3,label=label))
            ax.grid(alpha=.18)
            ax.ticklabel_format(style='plain',useOffset=False)
            ax.set_xlabel('Easting / local x (metres)')
            ax.set_ylabel('Northing / local y (metres)')
            ax.legend(handles=handles,loc='upper left',fontsize=8,ncol=min(4,len(handles)),framealpha=.95)
            ax.text(.98,.97,'N ↑',transform=ax.transAxes,ha='right',va='top',fontsize=14)
            fig.text(.5,.965,f'{row_id} · annotated task geometry',ha='center',fontsize=15)
            fig.text(.5,.035,'Offline metric renderer · no basemap · geometry: © OpenStreetMap contributors',ha='center',fontsize=9,color='#536678')
            fig.savefig(image_path,dpi=128)
            plt.close(fig)
            frame.update(scene_sha256=scene_hash(row,editing),image_sha256=sha256(image_path))
            write_json(target/f'{row_id}.frame.json',frame)
        counts[kind] = len(rows)
    return {'rendered':counts,'canvas':SIZE,'renderer':'offline_metric_v1'}

def decode_pixel_edit(task, row):
    from shapely import wkt
    from shapely.geometry import shape
    from edit_core.protocol import extract_json_obj
    payload = row.get('pixel_output')
    if payload is None:
        payload = extract_json_obj(row.get('prediction',''))
    if not isinstance(payload,dict):
        raise ValueError('Expected pixel-output JSON')
    pixel = shape(payload['geometry'])
    if pixel.is_empty or not pixel.is_valid or not all(math.isfinite(v) for v in pixel.bounds):
        raise ValueError('Invalid pixel geometry')
    frame = frame_for(task)
    geometry = from_pixels(pixel,frame)
    if payload.get('layer') is not None and (isinstance(payload['layer'],bool) or not float(payload['layer']).is_integer()):
        raise ValueError('Layer must be an integer ordinal value')
    ctx = {'predicted_F0_layer':payload['layer']} if payload.get('layer') is not None else {}
    gt_pixel = to_pixels(wkt.loads(task['ground_truth_wkt']),frame)
    return geometry,ctx,float(pixel.hausdorff_distance(gt_pixel))
