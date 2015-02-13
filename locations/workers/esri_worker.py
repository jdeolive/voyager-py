# (C) Copyright 2014 Voyager Search
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import division
import os
import sys
from collections import OrderedDict
import logging
import multiprocessing
import arcpy
from utils import status


status_writer = status.Writer()


class NullGeometry(Exception):
    pass

def global_job(*args):
    """Create a global job object."""
    global job
    job = args[0]


def update_row(fields, rows, row):
    """Updates the coded values in a row with the coded value descriptions."""
    field_domains = {f.name: f.domain for f in fields if f.domain}
    fields_values = zip(rows.fields, row)
    for j, x in enumerate(fields_values):
        if x[0] in field_domains:
            domain_name = field_domains[x[0]]
            row[j] = job.domains[domain_name][x[1]]
    return row


def query_layer(layer, spatial_rel='esriSpatialRelIntersects', where='1=1',
                out_fields='*', out_sr=4326, return_geometry=True):
    """Returns a GPFeatureRecordSetLayer from a feature service layer or
    a GPRecordSet form a feature service table.
    """
    import _server_admin as arcrest
    qry_layer = None
    if layer.type == 'Feature Layer':
        query = {'spatialRel': spatial_rel, 'where': where,
                 'outFields': out_fields, 'returnGeometry': return_geometry, 'outSR': out_sr}
        out = layer._get_subfolder('./query', arcrest.JsonResult, query)
        qry_layer = arcrest.gptypes.GPFeatureRecordSetLayer.fromJson(out._json_struct)
    else:
        query = {'where': where, 'outFields': out_fields, 'returnGeometry': return_geometry}
        out = layer._get_subfolder('./query', arcrest.JsonResult, query)
        qry_layer = arcrest.gptypes.GPRecordSet.fromJson(out._json_struct)
    return qry_layer


def make_feature(feature):
    """Makes a feature from a arcrest.geometry object."""
    geometry = None
    if isinstance(feature['geometry'], arcrest.geometry.Polyline):
        if feature['geometry'].paths:
            for path in feature['geometry'].paths:
                point_coords = [(pt.x, pt.y) for pt in path]
                point_array = arcpy.Array([arcpy.Point(*coords) for coords in point_coords])
                point_array.add(arcpy.Point())
            geometry = arcpy.Polyline(point_array)
    elif isinstance(feature['geometry'], arcrest.geometry.Polygon):
        if feature['geometry'].rings:
            for ring in feature['geometry'].rings:
                point_coords = [(pt.x, pt.y) for pt in ring]
                point_array = arcpy.Array([arcpy.Point(*coords) for coords in point_coords])
                point_array.add(arcpy.Point())
            geometry = arcpy.Polygon(point_array)
    elif isinstance(feature['geometry'], arcrest.geometry.Multipoint):
        if feature['geometry'].points:
            for point in feature['geometry'].points:
                point_coords = [(pt.x, pt.y) for pt in point]
                point_array = arcpy.Array([arcpy.Point(*coords) for coords in point_coords])
                point_array.add(arcpy.Point())
            geometry = arcpy.Multipoint(point_array)

    if geometry:
        return geometry
    else:
        raise NullGeometry


def index_service(url):
    """Index the records in Map and Feature Services."""
    try:
        import _server_admin as arcrest
    except ImportError as ie:
        status_writer.send_state(status.STAT_FAILED, ie.message)
        return

    job.connect_to_zmq()
    geo = {}
    entry = {}
    mapped_attributes = OrderedDict()
    service = arcrest.FeatureService(url)

    layers = service.layers + service.tables

    for layer in layers:
        ql = query_layer(layer)
        if not ql.features:
            status_writer.send_status("Layer {0} has no features.".format(layer.name))
            continue
        if 'attributes' in ql.features[0]:
            attributes = ql.features[0]['attributes']
        else:
            status_writer.send_status("Layer {0} has no attributes.".format(layer.name))

        status_writer.send_status('Indexing {0}...'.format(layer.name))
        if layer.type == 'Feature Layer':
            geo['srid'] = ql.spatialReference.wkid
        if not job.fields_to_keep == ['*']:
            for fk in job.fields_to_keep:
                mapped_fields = dict((name, val) for name, val in attributes.items() if fk in name)
                if job.fields_to_skip:
                    for fs in job.fields_to_skip:
                        [mapped_fields.pop(name) for name in attributes if name in fs]
        else:
            mapped_fields = attributes

        job.tables_to_keep()  # This call will generate the field mapping dictionary.
        if 'map' in job.field_mapping[0]:
            field_map = job.field_mapping[0]['map']
            for k, v in mapped_fields.items():
                if k in field_map:
                    new_field = field_map[k]
                    mapped_attributes[new_field] = mapped_fields.pop(k)
                else:
                    field_type = job.default_mapping(type(v))
                    mapped_attributes[field_type + k] = mapped_fields.pop(k)
        else:
            for k, v in mapped_fields.items():
                field_type = job.default_mapping(type(v))
                mapped_attributes[field_type + k] = mapped_fields.pop(k)

        row_count = len(ql.features)
        if layer.type == 'Table':
            for i, row in enumerate(ql.features):
                entry['id'] = '{0}_{1}_{2}'.format(job.location_id, layer.name, i)
                entry['location'] = job.location_id
                entry['action'] = job.action_type
                mapped_fields = dict(zip(mapped_attributes.keys(), row['attributes'].values()))
                entry['entry'] = {'fields': mapped_fields}
                job.send_entry(entry)
                status_writer.send_percent(i / row_count, "{0} {1:%}".format(layer.name, i / row_count), 'esri_worker')
        else:
            if isinstance(ql.features[0]['geometry'], arcrest.geometry.Point):
                for i, feature in enumerate(ql.features):
                    pt = feature['geometry']
                    geo['lon'] = pt.x
                    geo['lat'] = pt.y
                    entry['id'] = '{0}_{1}_{2}'.format(job.location_id, layer.name, i)
                    entry['location'] = job.location_id
                    entry['action'] = job.action_type
                    mapped_fields = dict(zip(mapped_attributes.keys(), feature['attributes'].values()))
                    entry['entry'] = {'geo': geo, 'fields': mapped_fields}
                    job.send_entry(entry)
                    status_writer.send_percent(i / row_count, "{0} {1:%}".format(layer.name, i / row_count), 'esri_worker')
            else:
                for i, feature in enumerate(ql.features):
                    try:
                        geometry = make_feature(feature)  # Catch possible null geometries.
                    except NullGeometry:
                        continue
                    geo['xmin'], geo['xmax'] = geometry.extent.XMin, geometry.extent.YMax
                    geo['ymin'], geo['ymax'] = geometry.extent.YMin, geometry.extent.YMax
                    entry['id'] = '{0}_{1}_{2}'.format(job.location_id, layer.name, i)
                    entry['location'] = job.location_id
                    entry['action'] = job.action_type
                    mapped_fields = dict(zip(mapped_attributes.keys(), feature['attributes'].values()))
                    entry['entry'] = {'geo': geo, 'fields': mapped_fields}
                    job.send_entry(entry)
                    status_writer.send_percent(i / row_count, "{0} {1:%}".format(layer.name, i / row_count), 'esri_worker')


def worker(data_path, esri_service=False):
    """The worker function to index feature data and tabular data."""
    if esri_service:
        index_service(data_path)
    else:
        job.connect_to_zmq()
        geo = {}
        entry = {}
        dsc = arcpy.Describe(data_path)

        if dsc.dataType == 'Table':
            field_types = job.search_fields(data_path)
            fields = field_types.keys()
            query = job.get_table_query(dsc.name)
            constraint = job.get_table_constraint(dsc.name)
            if query and constraint:
                expression = """{0} AND {1}""".format(query, constraint)
            else:
                if query:
                    expression = query
                else:
                    expression = constraint
            mapped_fields = job.map_fields(dsc.name, fields, field_types)
            with arcpy.da.SearchCursor(data_path, fields, expression) as rows:
                for i, row in enumerate(rows, 1):
                    if job.domains:
                        row = update_row(dsc.fields, rows, list(row))
                    mapped_fields = dict(zip(mapped_fields, row))
                    mapped_fields['_discoveryID'] = job.discovery_id
                    mapped_fields['title'] = dsc.name
                    oid_field = filter(lambda x: x in ('FID', 'OID', 'OBJECTID'), rows.fields)
                    if oid_field:
                        fld_index = rows.fields.index(oid_field[0])
                    else:
                        fld_index = i
                    entry['id'] = '{0}_{1}_{2}'.format(job.location_id, os.path.basename(data_path), row[fld_index])
                    entry['location'] = job.location_id
                    entry['action'] = job.action_type
                    entry['entry'] = {'fields': mapped_fields}
                    job.send_entry(entry)
        else:
            sr = arcpy.SpatialReference(4326)
            geo['spatialReference'] = dsc.spatialReference.name
            geo['code'] = dsc.spatialReference.factoryCode
            field_types = job.search_fields(dsc.catalogPath)
            fields = field_types.keys()
            query = job.get_table_query(dsc.name)
            constraint = job.get_table_constraint(dsc.name)
            if query and constraint:
                expression = """{0} AND {1}""".format(query, constraint)
            else:
                if query:
                    expression = query
                else:
                    expression = constraint
            if dsc.shapeFieldName in fields:
                fields.remove(dsc.shapeFieldName)
                field_types.pop(dsc.shapeFieldName)
            if dsc.shapeType == 'Point':
                with arcpy.da.SearchCursor(dsc.catalogPath, ['SHAPE@'] + fields, expression, sr) as rows:
                    mapped_fields = job.map_fields(dsc.name, list(rows.fields[1:]), field_types)
                    for i, row in enumerate(rows):
                        if job.domains:
                            row = update_row(dsc.fields, rows, list(row))
                        geo['lon'] = row[0].firstPoint.X #row[0][0]
                        geo['lat'] = row[0].firstPoint.Y #row[0][1]
                        if job.include_wkt:
                            geo['wkt'] = row[0].WKT
                        mapped_fields = dict(zip(mapped_fields, row[1:]))
                        mapped_fields['_discoveryID'] = job.discovery_id
                        mapped_fields['title'] = dsc.name
                        mapped_fields['geometry_type'] = dsc.shapeType
                        entry['id'] = '{0}_{1}_{2}'.format(job.location_id, os.path.basename(data_path), i)
                        entry['location'] = job.location_id
                        entry['action'] = job.action_type
                        entry['entry'] = {'geo': geo, 'fields': mapped_fields}
                        job.send_entry(entry)
            else:
                with arcpy.da.SearchCursor(dsc.catalogPath, ['SHAPE@'] + fields, expression, sr) as rows:
                    mapped_fields = job.map_fields(dsc.name, list(rows.fields[1:]), field_types)
                    for i, row in enumerate(rows):
                        if job.domains:
                            row = update_row(dsc.fields, rows, list(row))
                        geo['xmin'] = row[0].extent.XMin
                        geo['xmax'] = row[0].extent.XMax
                        geo['ymin'] = row[0].extent.YMin
                        geo['ymax'] = row[0].extent.YMax
                        if job.include_wkt:
                            geo['wkt'] = row[0].WKT
                        mapped_fields = dict(zip(mapped_fields, row[1:]))
                        mapped_fields['_discoveryID'] = job.discovery_id
                        mapped_fields['title'] = dsc.name
                        mapped_fields['geometry_type'] = dsc.shapeType
                        entry['id'] = '{0}_{1}_{2}'.format(job.location_id, os.path.basename(data_path), i)
                        entry['location'] = job.location_id
                        entry['action'] = job.action_type
                        entry['entry'] = {'geo': geo, 'fields': mapped_fields}
                        job.send_entry(entry)


def run_job(esri_job):
    """Determines the data type and each dataset is sent to the worker to be processed."""
    status_writer.send_percent(0.0, "Initializing... 0.0%", 'esri_worker')
    job = esri_job

    if job.path.startswith('http'):
        global_job(job)
        worker(job.path, esri_service=True)
        return

    dsc = arcpy.Describe(job.path)

    # A single feature class or table.
    if dsc.dataType in ('DbaseTable', 'FeatureClass', 'Shapefile', 'Table'):
        global_job(job, int(arcpy.GetCount_management(job.path).getOutput(0)))
        worker(job.path)
        return

    # A geodatabase (.mdb, .gdb, or .sde).
    elif dsc.dataType == 'Workspace':
        arcpy.env.workspace = job.path
        feature_datasets = arcpy.ListDatasets('*', 'Feature')
        tables = []
        tables_to_keep = job.tables_to_keep()
        tables_to_skip = job.tables_to_skip()
        if job.tables_to_keep:
            for t in tables_to_keep:
                [tables.append(os.path.join(job.path, tbl)) for tbl in arcpy.ListTables(t)]
                [tables.append(os.path.join(job.path, fc)) for fc in arcpy.ListFeatureClasses(t)]
                for fds in feature_datasets:
                    [tables.append(os.path.join(job.path, fds, fc)) for fc in arcpy.ListFeatureClasses(wild_card=t, feature_dataset=fds)]
        else:
            [tables.append(os.path.join(job.path, tbl)) for tbl in arcpy.ListTables()]
            [tables.append(os.path.join(job.path, fc)) for fc in arcpy.ListFeatureClasses()]
            for fds in feature_datasets:
                [tables.append(os.path.join(job.path, fds, fc)) for fc in arcpy.ListFeatureClasses(feature_dataset=fds)]

        if tables_to_skip:
            for t in tables_to_keep:
                [tables.remove(os.path.join(job.path, tbl)) for tbl in arcpy.ListTables(t)]
                [tables.remove(os.path.join(job.path, fc)) for fc in arcpy.ListFeatureClasses(t)]
                for fds in feature_datasets:
                    [tables.remove(os.path.join(job.path, fds, fc)) for fc in arcpy.ListFeatureClasses(wild_card=t, feature_dataset=fds)]

    # A geodatabase feature dataset, SDC data, or CAD dataset.
    elif dsc.dataType == 'FeatureDataset' or dsc.dataType == 'CadDrawingDataset':
        tables_to_keep = job.tables_to_keep()
        tables_to_skip = job.tables_to_skip()
        arcpy.env.workspace = job.path
        if tables_to_keep:
            tables = []
            for tbl in tables_to_keep:
                [tables.append(os.path.join(job.path, fc)) for fc in arcpy.ListFeatureClasses(tbl)]
                tables = list(set(tables))
        else:
            tables = [os.path.join(job.path, fc) for fc in arcpy.ListFeatureClasses()]
        if tables_to_skip:
            for tbl in tables_to_skip:
                [tables.remove(os.path.join(job.path, fc)) for fc in arcpy.ListFeatureClasses(tbl) if fc in tables]

    # Not a recognized data type.
    else:
        sys.exit(1)

    if job.multiprocess:
        # Multiprocess larger databases and feature datasets.
        multiprocessing.log_to_stderr()
        logger = multiprocessing.get_logger()
        logger.setLevel(logging.INFO)
        pool = multiprocessing.Pool(initializer=global_job, initargs=(job,))
        for i, _ in enumerate(pool.imap_unordered(worker, tables), 1):
            status_writer.send_percent(i / len(tables), "{0:%}".format(i / len(tables)), 'esri_worker')
        # Synchronize the main process with the job processes to ensure proper cleanup.
        pool.close()
        pool.join()
    else:
        for i, tbl in enumerate(tables, 1):
            global_job(job)
            worker(tbl)
            status_writer.send_percent(i / len(tables), "{0} {1:%}".format(tbl, i / len(tables)), 'esri_worker')
    return