import vigra
import vigra.graphs as graphs
import numpy as np
from concurrent import futures
import os

from ..ExperimentSettings import ExperimentSettings
from ..MCSolverImpl import multicut_exact, weight_z_edges, weight_all_edges, weight_xyz_edges, multicut_fusionmoves
from ..tools import find_matching_row_indices, find_matching_row_indices_fast

from ..tools import cacher_hdf5, cache_name

import logging
logger = logging.getLogger(__name__)

# if build from source and not a conda pkg, we assume that we have cplex
try:
    import nifty
except ImportError:
    try:
        import nifty_with_cplex as nifty  # conda version build with cplex
    except ImportError:
        try:
            import nifty_with_gurobi as nifty  # conda version build with gurobi
        except ImportError:
            raise ImportError("No valid nifty version was found.")


# calculate the distance transform for the given segmentation
def distance_transform(segmentation, anisotropy):
    edge_volume = np.concatenate(
        [vigra.analysis.regionImageToEdgeImage(segmentation[z])[None, :]
         for z in xrange(segmentation.shape[0])],
        axis=0
    )
    dt = vigra.filters.distanceTransform(edge_volume.astype('uint32'), pixel_pitch=anisotropy, background=True)
    return dt


def extract_local_graph_from_segmentation(
        ds,
        seg_id,
        mc_segmentation,
        object_id,
        uv_ids,
        uv_ids_lifted=None
):

    seg = ds.seg(seg_id)
    mask = mc_segmentation == object_id
    seg_ids = np.unique(seg[mask])

    # map the extracted seg_ids to consecutive labels
    seg_ids_local, _, mapping = vigra.analysis.relabelConsecutive(
        seg_ids,
        start_label=0,
        keep_zeros=False
    )
    # mapping = old to new,
    # reverse = new to old
    reverse_mapping = {val: key for key, val in mapping.iteritems()}

    # mask the local uv ids in this object
    local_uv_mask = np.in1d(uv_ids, seg_ids)
    local_uv_mask = local_uv_mask.reshape(uv_ids.shape).all(axis=1)

    if uv_ids_lifted is not None:
        # mask the lifted uv ids in this object
        lifted_uv_mask = np.in1d(uv_ids_lifted, seg_ids)
        lifted_uv_mask = lifted_uv_mask.reshape(uv_ids_lifted.shape).all(axis=1)
        return local_uv_mask, lifted_uv_mask, mapping, reverse_mapping

    return local_uv_mask, mapping, reverse_mapping


# TODO take nifty shortest paths and parallelize
def shortest_paths(
        indicator,
        pairs,
        n_threads=1
):
    """
    This function was copied from processing_lib.py
    :param indicator:
    :return:
    """

    gridgr         = graphs.gridGraph(indicator.shape)
    gridgr_edgeind = graphs.implicitMeanEdgeMap(gridgr, indicator.astype('float32'))

    def single_path(pair, instance=None):
        source = pair[0]
        target = pair[1]
        logger.info('Calculating path from {} to {}'.format(source, target))
        if instance is None:
            instance = graphs.ShortestPathPathDijkstra(gridgr)

        targetNode = gridgr.coordinateToNode(target)
        sourceNode = gridgr.coordinateToNode(source)

        instance.run(gridgr_edgeind, sourceNode, target=targetNode)
        path = instance.path(pathType='coordinates')
        if path.any():
            return path

    if n_threads > 1:
        print "Multi-threaded w/ n-threads = ", n_threads
        with futures.ThreadPoolExecutor(max_workers=n_threads) as executor:
            tasks = [executor.submit(single_path, pair) for pair in pairs]
            paths = [t.result() for t in tasks]
    else:
        print "Single threaded"
        instance = graphs.ShortestPathPathDijkstra(gridgr)
        paths = [single_path(pair, instance) for pair in pairs]

    return paths


# convenience function to combine path features
# TODO code different features with some keys
# TODO include seg_id
def path_feature_aggregator(
        ds, paths,
        feature_list=None,
        mc_segmentation=None, paths_to_objs=None, train_sets=None,
        path_to_edge_features = None
):


    # FIXME
    seg_id = 0

    def make_objs_to_paths_dict(paths_to_objs, paths):

        objs_to_paths = {}
        for obj_id, obj in enumerate(paths_to_objs):

            if obj in objs_to_paths.keys():
                objs_to_paths[obj][obj_id] = paths[obj_id]
            else:
                objs_to_paths[obj] = {obj_id: paths[obj_id]}

        return objs_to_paths

    # The actual default values of the feature list
    if feature_list is None:
        feature_list = ['path_features', 'lengths', 'multicuts', 'cut_features']

    logger.info('Starting path_feature_aggregator with features: {} ...'.format(feature_list))

    anisotropy_factor = ExperimentSettings().anisotropy_factor

    feature_space = []

    for feature in feature_list:

        if feature == 'path_features':

            logger.debug('Computing path features ...')

            feature_space.append(path_features_from_feature_images(ds, 0, paths, anisotropy_factor))
            feature_space.append(path_features_from_feature_images(ds, 1, paths, anisotropy_factor))
            # we assume that the distance transform is added as inp_id 2
            feature_space.append(path_features_from_feature_images(ds, 2, paths, anisotropy_factor))

            logger.debug('... done computing path features!')

        if feature == 'lengths':

            logger.debug('Computing path lengths ...')

            feature_space.append(compute_path_lengths(paths, [anisotropy_factor, 1., 1.]))

            logger.debug('... done computing path lengths!')

        if feature == 'multicuts':

            logger.debug('Computing multicut path features ...')

            # Make sure all necessary information is supplied
            assert mc_segmentation is not None, 'Supply a multicut segmentation when using multicut path features!'
            assert paths_to_objs is not None, 'Supply an object to path dictionary when using multicut path features!'

            # Convert paths_to_objs list to objs_to_paths dictionary
            objs_to_paths = make_objs_to_paths_dict(paths_to_objs, paths)

            if not ExperimentSettings().use_probs_map_for_cut_features:
                assert train_sets is not None, 'Supply train sets when using multicut path features!'
                # FIXME consider integrating the feature list into ExperimentSettings
                from multicut_src import learn_and_predict_rf_from_gt
                edge_probabilities = learn_and_predict_rf_from_gt(
                    train_sets, ds, seg_id, seg_id, ['raw', 'prob', 'reg'],
                    with_defects=False,
                    use_2rfs=ExperimentSettings().use_2rfs
                )

            else:
                # TODO: Load edge features and take first channel (see notes)
                assert path_to_edge_features is not None, 'Supply a path to edge features when using multicut path features!'
                edge_probabilities = vigra.readHDF5(path_to_edge_features, 'data')[:, 0]

            feature_space.append(multicut_path_features(
                ds,
                seg_id,
                mc_segmentation,
                objs_to_paths,  # dict[merge_ids : dict[path_ids : paths]]
                edge_probabilities
            ))

            logger.debug('... done computing multicut path features!')

        if feature == 'cut_features':

            logger.debug('Computing cut features ...')

            # Make sure all necessary information is supplied
            assert mc_segmentation is not None, 'Supply a multicut segmentation when using cut features!'
            assert paths_to_objs is not None, 'Supply an object to path dictionary when using cut features!'

            # Convert paths_to_objs list to objs_to_paths dictionary
            objs_to_paths = make_objs_to_paths_dict(paths_to_objs, paths)

            if not ExperimentSettings().use_probs_map_for_cut_features:
                assert train_sets is not None, 'Supply train sets when using cut features!'
                # FIXME consider integrating the feature list into ExperimentSettings
                from multicut_src import learn_and_predict_rf_from_gt
                edge_probabilities = learn_and_predict_rf_from_gt(
                    train_sets, ds, seg_id, seg_id, ['raw', 'prob', 'reg'],
                    with_defects=False,
                    use_2rfs=ExperimentSettings().use_2rfs
                )

            else:
                # TODO: Load edge features and take first channel (see notes)
                assert path_to_edge_features is not None, 'Supply a path to edge features when using cut features!'
                edge_probabilities = vigra.readHDF5(path_to_edge_features, 'data')[:, 0]

            feature_space.append(cut_features(
                ds, seg_id, mc_segmentation, objs_to_paths,
                edge_probabilities,
                ExperimentSettings().anisotropy_factor,
                ['raw', 'prob', 'distance_transform'],
                'watershed'
            ))

            logger.debug('... done computing cut features!')

    logger.info('Finished path_feature_aggregator!')

    return np.concatenate(feature_space, axis=1)


# TODO this could be parallelized over the paths
# compute the path lens for all paths
def compute_path_lengths(paths, anisotropy):
    """
    Computes the length of a path

    :param path:
        list( np.array([[x11, x12, ..., x1n], [x21, x22, ..., x2n], ..., [xm1, xm2, ..., xmn]]) )
        with n dimensions and m coordinates
    :param anisotropy: [a1, a2, ..., an]
    :return: path lengtht list(float)
    """
    def compute_path_length(path, aniso_temp):
        # pathlen = 0.
        # for i in xrange(1, len(path)):
        #    add2pathlen = 0.
        #    for j in xrange(0, len(path[0, :])):
        #        add2pathlen += (anisotropy[j] * (path[i, j] - path[i - 1, j])) ** 2

        #    pathlen += add2pathlen ** (1. / 2)
        # TODO check that this actually agrees
        path_euclidean_diff = aniso_temp * np.diff(path, axis=0)
        path_euclidean_diff = np.sqrt(np.sum(np.square(path_euclidean_diff), axis=1))
        return np.sum(path_euclidean_diff, axis=0)
    aniso_temp = np.array(anisotropy)
    return np.array([compute_path_length(np.array(path), aniso_temp) for path in paths])[:, None]


# don't cache for now
@cacher_hdf5("feature_folder", ignoreNumpyArrays=False)
def path_features_from_feature_images(
        ds,
        inp_id,
        paths,
        anisotropy_factor):

    # FIXME for now we don't use fastfilters here
    feat_paths = ds.make_filters(inp_id, anisotropy_factor)
    # print feat_paths
    # TODO sort the feat_path correctly
    # load the feature images ->
    # FIXME this might be too memory hungry if we have a large global bounding box

    # compute the global bounding box
    global_min = np.min(
        np.concatenate([np.min(path, axis=0)[None, :] for path in paths], axis=0),
        axis=0
    )
    global_max = np.max(
        np.concatenate([np.max(path, axis=0)[None, :] for path in paths], axis=0),
        axis=0
    ) + 1
    # substract min coords from all paths to bring them to new coordinates
    paths_in_roi = [path - global_min for path in paths]
    roi = np.s_[
        global_min[0]:global_max[0],
        global_min[1]:global_max[1],
        global_min[2]:global_max[2]
    ]

    # load features in global boundng box
    feature_volumes = []
    import h5py
    for path in feat_paths:
        with h5py.File(path) as f:
            feat_shape = f['data'].shape
            # we add a singleton dimension to single channel features to loop over channel later
            if len(feat_shape) == 3:
                feature_volumes.append(f['data'][roi][..., None])
            else:
                feature_volumes.append(f['data'][roi])
    stats = ExperimentSettings().feature_stats

    def extract_features_for_path(path):

        # calculate the local path bounding box
        min_coords  = np.min(path, axis=0)
        max_coords = np.max(path, axis=0)
        max_coords += 1
        shape = tuple(max_coords - min_coords)
        path_image = np.zeros(shape, dtype='uint32')
        path -= min_coords
        # we swapaxes to properly index the image properly
        path_sa = np.swapaxes(path, 0, 1)
        path_image[path_sa[0], path_sa[1], path_sa[2]] = 1

        path_features = []
        for feature_volume in feature_volumes:
            for c in range(feature_volume.shape[-1]):
                path_roi = np.s_[
                    min_coords[0]:max_coords[0],
                    min_coords[1]:max_coords[1],
                    min_coords[2]:max_coords[2],
                    c  # wee need to also add the channel to the slicing
                ]
                extractor = vigra.analysis.extractRegionFeatures(
                    feature_volume[path_roi],
                    path_image,
                    ignoreLabel=0,
                    features=stats
                )
                # TODO make sure that dimensions match for more that 1d stats!
                path_features.extend(
                    [extractor[stat][1] for stat in stats]
                )
        # ret = np.array(path_features)[:,None]
        # print ret.shape
        return np.array(path_features)[None, :]

    if len(paths) > 1:

        # We parallelize over the paths for now.
        # TODO parallelizing over filters might in fact be much faster, because
        # we avoid the single threaded i/o in the beginning!
        # it also lessens memory requirements if we have less threads than filters
        # parallel
        with futures.ThreadPoolExecutor(max_workers=ExperimentSettings().n_threads) as executor:
            tasks = []
            for p_id, path in enumerate(paths_in_roi):
                tasks.append(executor.submit(extract_features_for_path, path))
            out = np.concatenate([t.result() for t in tasks], axis=0)

    else:

        out = np.concatenate([extract_features_for_path(path) for path in paths_in_roi])

    # serial for debugging
    # out = []
    # for p_id, path in enumerate(paths_in_roi):
    #     out.append( extract_features_for_path(path) )
    # out = np.concatenate(out, axis = 0)

    assert out.ndim == 2, str(out.shape)
    assert out.shape[0] == len(paths), str(out.shape)
    # TODO checkfor correct number of features

    return out


def make_local_uv(seg, mc_segmentation, uv_ids, obj_id):

    mask = mc_segmentation == obj_id

    seg_ids = np.unique(seg[mask])

    # map the extracted seg_ids to consecutive labels
    seg_ids_local, _, mapping = vigra.analysis.relabelConsecutive(
        seg_ids,
        start_label=0,
        keep_zeros=False
    )

    # mapping = old to new,
    # reverse = new to old
    # reverse_mapping = {val: key for key, val in mapping.iteritems()}

    # mask the local uv ids in this object
    local_uv_mask = np.in1d(uv_ids, seg_ids)
    local_uv_mask = local_uv_mask.reshape(uv_ids.shape).all(axis=1)

    uv_local = np.array([[mapping[u] for u in uv] for uv in uv_ids[local_uv_mask]])
    n_var = uv_local.max() + 1

    return local_uv_mask, mapping, uv_local, n_var

    # reverse_mapping = {val: key for key, val in mapping.iteritems()}
    # save_im = np.zeros(mc_segmentation.shape)
    # for item in seg_ids:
    #     # save_im[seg == reverse_mapping[item]] = 1
    #     save_im[seg == item] = 1
    #
    # node_labels = seg_ids_local
    # obj_id=6
    # # Make global list of node labels initialized to zero
    # node_labels_global = np.zeros((uv_ids.max() + 1,))
    # # Set the values of the local nodes to the respective label
    # reverse_mapping = {val: key for key, val in mapping.iteritems()}
    # for node_label_id, node_label in enumerate(node_labels):
    #     node_labels_global[reverse_mapping[node_label_id]] = node_label + 1
    # split_obj_im = ds.project_mc_result(0, node_labels_global)
    # save_filepath = '/export/home/jhennies/debug/cut_features/{}_{}_{}.h5'.format(ds.ds_name, obj_id, path_id)
    # vigra.writeHDF5(split_obj_im, save_filepath, 'data', compression='gzip')



@cacher_hdf5("feature_folder", ignoreNumpyArrays=False)
def multicut_path_features(
        ds,
        seg_id,
        mc_segmentation,
        objs_to_paths,  # dict[merge_ids : dict[path_ids : paths]]
        edge_probabilities
):

    logger.debug('ds.ds_name = {}'.format(ds.ds_name))
    logger.debug('seg_id = {}'.format(seg_id))
    logger.debug('mc_segmentation.shape = {}'.format(mc_segmentation.shape))
    logger.debug('len(objs_to_paths) = {}'.format(len(objs_to_paths)))
    logger.debug('len(edge_probabilities = {}'.format(len(edge_probabilities)))

    uv_ids = ds.uv_ids(seg_id)

    # needed for weight transformation
    weighting_scheme = ExperimentSettings().weighting_scheme
    weight           = ExperimentSettings().weight
    edge_areas       = ds.topology_features(seg_id, False)[:, 0].astype('uint32')
    edge_indications = ds.edge_indications(seg_id)

    # TODO more_feats ?!
    betas = np.arange(0.3, 0.75, 0.05)
    n_feats = len(betas)
    n_paths = np.sum([len(paths) for _, paths in objs_to_paths.iteritems()])
    features = np.zeros((n_paths, n_feats), dtype='float32')

    # transform edge-probabilities to weights
    def to_weights(edge_mask, beta):

        probs = edge_probabilities[edge_mask]
        areas = edge_areas[edge_mask]
        indications = edge_indications[edge_mask]

        # scale the probabilities
        p_min = 0.001
        p_max = 1. - p_min
        probs = (p_max - p_min) * probs + p_min

        # probabilities to energies, second term is boundary bias
        weights = np.log((1. - probs) / probs) + np.log((1. - beta) / beta)

        # weight edges
        if weighting_scheme == "z":
            weights = weight_z_edges(weights, areas, indications, weight)
        elif weighting_scheme == "xyz":
            weights = weight_xyz_edges(weights, areas, indications, weight)
        elif weighting_scheme == "all":
            weights = weight_all_edges(weights, areas, weight)

        return weights

    # 1. ---------------------------
    logger.debug('multicut_path_features, step 1 #####################')

    seg = ds.seg(seg_id)
    mappings = {}
    local_uv_masks = {}
    n_vars = {}
    uv_locals = {}

    if ExperimentSettings().n_threads == 1:

        for obj_id in objs_to_paths:
            local_uv_masks[obj_id], mappings[obj_id], uv_locals[obj_id], n_vars[obj_id] = make_local_uv(
                seg, mc_segmentation, uv_ids, obj_id
            )

    else:

        with futures.ThreadPoolExecutor(max_workers=ExperimentSettings().n_threads) as executor:
            tasks = {}
            for obj_id in objs_to_paths:
                tasks[obj_id] = executor.submit(make_local_uv, seg, mc_segmentation, uv_ids, obj_id)
            for obj_id in objs_to_paths:
                local_uv_masks[obj_id], mappings[obj_id], uv_locals[obj_id], n_vars[obj_id] = tasks[obj_id].result()

    # 2. ---------------------------
    logger.debug('multicut_path_features, step 2 #####################')

    path_edge_ids_s = {}

    # find the local edge ids along the path
    def edges_along_path(seg, path, mapping, uvs_local):
        edge_ids = []
        u = mapping[seg[tuple(path[0])]]
        for p in path[1:]:
            v = mapping[seg[tuple(p)]]
            if u != v:
                uv = np.array([[min(u, v), max(u, v)]])
                # FIXME Replace by faster version
                edge_id = find_matching_row_indices_fast(uvs_local, uv)[0]
                edge_ids.append(edge_id)
            u = v
        return np.array(edge_ids)

    def get_path_edge_ids(seg, objs_to_path, mapping, uv_local):

        path_edge_ids = {}

        for path_id, path in objs_to_path.iteritems():
            path_edge_ids[path_id] = edges_along_path(seg, path, mapping, uv_local)

        return path_edge_ids

    # FIXME get_path_edge_ids doesn't parallelize
    # if ExperimentSettings().n_threads == 1:
    if True:

        for obj_id in objs_to_paths:
            # logger.debug('    obj_id = {}'.format(obj_id))
            path_edge_ids_s[obj_id] = get_path_edge_ids(seg, objs_to_paths[obj_id], mappings[obj_id], uv_locals[obj_id])

    else:

        with futures.ThreadPoolExecutor(max_workers=ExperimentSettings().n_threads) as executor:
            tasks = {}
            for obj_id in objs_to_paths:
                tasks[obj_id] = executor.submit(get_path_edge_ids, seg, objs_to_paths[obj_id], mappings[obj_id], uv_locals[obj_id])
            for obj_id in objs_to_paths:
                path_edge_ids_s[obj_id] = tasks[obj_id].result()

    # 3. ---------------------------
    logger.debug('multicut_path_features, step 3 #####################')
    cut_edges_s_s = {}

    def get_cut_edges(betas, local_uv_mask, n_var, uv_local, mapping):

        cut_edges_s = {}

        # logger.debug('---> for ii, beta in enumerate(betas): ...')
        for ii, beta in enumerate(betas):

            # logger.debug('beta = {}'.format(beta))

            # logger.debug('------> ii = {}'.format(ii))
            weights = to_weights(local_uv_mask, beta)
            # FIXME This takes forever for some objects
            # node_labels, _, _ = multicut_exact(n_var, uv_local, weights)
            # FIXME Use this instead?
            node_labels, _, _ = multicut_fusionmoves(
                n_var,
                uv_local,
                weights,
                n_threads=ExperimentSettings().n_threads,
                solver_backend='kl'
            )
            cut_edges = node_labels[[uv_local[:, 0]]] != node_labels[[uv_local[:, 1]]]

            cut_edges_s[ii] = cut_edges

            # # TODO: For debug purposes:
            # # TODO: Restore the segmentation and save it as volume
            # # Make global list of node labels initialized to zero
            # node_labels_global = np.zeros((uv_ids.max() + 1,))
            # # Set the values of the local nodes to the respective label
            # reverse_mapping = {val: key for key, val in mapping.iteritems()}
            # for node_label_id, node_label in enumerate(node_labels):
            #     node_labels_global[reverse_mapping[node_label_id]] = node_label + 1
            # split_obj_im = ds.project_mc_result(0, node_labels_global)
            # save_filepath = '/export/home/jhennies/debug/{}_{}_{}.h5'.format(ds.ds_name, obj_id, beta)
            # vigra.writeHDF5(split_obj_im, save_filepath, 'data', compression='gzip')

        return cut_edges_s

    # if ExperimentSettings().n_threads == 1:
    if True:

        for obj_id in objs_to_paths:
            # logger.debug('---- obj_id = {}'.format(obj_id))
            cut_edges_s_s[obj_id] = get_cut_edges(betas, local_uv_masks[obj_id], n_vars[obj_id], uv_locals[obj_id], mappings[obj_id])

    else:

        with futures.ThreadPoolExecutor(max_workers=ExperimentSettings().n_threads) as executor:
            tasks = {}
            for obj_id in objs_to_paths:
                tasks[obj_id] = executor.submit(get_cut_edges, betas, local_uv_masks[obj_id], n_vars[obj_id],
                                                uv_locals[obj_id], mappings[obj_id])
            for obj_id in objs_to_paths:
                cut_edges_s_s[obj_id] = tasks[obj_id].result()

    # 4. ---------------------------
    logger.debug('multicut_path_features, step 4 #####################')

    for obj_id in objs_to_paths:
        # logger.debug('    obj_id = {}'.format(obj_id))

        # logger.debug('---> for ii, beta in enumerate(betas): ...')
        for ii, beta in enumerate(betas):

            # logger.debug('------> for path_id, i_ids in path_edge_ids.iteritems(): ...')
            for path_id, e_ids in path_edge_ids_s[obj_id].iteritems():
                # logger.debug('---------> path_id = {}'.format(path_id))
                # e_ids is zero if the path is very short and doesn't cross superpixel edges
                # This should happen very rarely
                if len(e_ids) > 0:
                    features[path_id, ii] = np.sum(cut_edges_s_s[obj_id][ii][e_ids])
                else:
                    # logger.warning(
                    #     'Path not crossing superpixel edges detected for obj_id = {}, beta = {}, path_id = {}'.format(
                    #         obj_id, beta, path_id))
                    features[path_id, ii] = 0

    logger.debug('End of multicut_path_features')
    return features


def cut_watershed(graph, weights, source, sink):

    # TODO I don't know if this is the correct way to do this
    # make the seeds from source and sink
    seeds = np.zeros(graph.numberOfNodes, dtype='uint64')
    seeds[source] = 1
    seeds[sink] = 2
    node_labeling = nifty.graph.edgeWeightedWatershedsSegmentation(
        graph,
        seeds,
        weights
    )

    uvs = graph.uvIds()
    # return the edge labels (cut or not cut for each edge)
    return node_labeling[uvs[:, 0]] != node_labeling[uvs[:, 1]], node_labeling


def cut_graphcut(graph, weights, source, sink):
    raise NotImplementedError("Cutting with graph cut is too cutting edge.")


def cut_seeded_agglomeration(graph, weights, source, sink):
    raise NotImplementedError("Cutting with seeded agglo not implemented yet.")


# features based on most likely cut along path (via graphcut)
# return edge features of corresponding cut, depending on feature list
@cacher_hdf5("feature_folder", ignoreNumpyArrays=False)
def cut_features(
        ds,
        seg_id,
        mc_segmentation,
        objs_to_paths,  # dict[merge_ids : dict[path_ids : paths]]
        edge_probabilities,
        anisotropy_factor,
        feat_list,
        cut_method
):

    if feat_list is None:
        feat_list = ['raw', 'prob', 'distance_transform']
    if cut_method is None:
        cut_method = 'watershed'

    logger.debug('Start cut features')

    cutters = {
        'watershed': cut_watershed,
        'graphcut': cut_graphcut,
        'seeded_clustering': cut_seeded_agglomeration
    }
    assert feat_list
    assert cut_method in cutters
    cutter = cutters[cut_method]

    p_min = 0.001
    p_max = 1. - p_min
    edge_weights = (p_max - p_min) * edge_probabilities + p_min
    edge_weights = np.log((1 - edge_weights) / edge_weights)

    seg = ds.seg(seg_id)
    uv_ids = ds.uv_ids(seg_id)


    # find the global and local edge ids along the path, as well as the local
    # start and ed point of the path
    def edges_along_path(path, mapping, uvs_local):

        us_p = np.array([seg[tuple(p)] for p in path[:-1]])
        us_p_local = np.array([mapping[seg[tuple(p)]] for p in path[:-1]])

        vs_p = np.array([seg[tuple(p)] for p in path[1:]])
        vs_p_local = np.array([mapping[seg[tuple(p)]] for p in path[1:]])

        # Positions where vs and us differ
        diff_mask = (vs_p - us_p).astype('bool')

        # Mask the us, vs, us_local, and vs_local
        us_p = us_p[diff_mask]
        vs_p = vs_p[diff_mask]
        us_p_local = us_p_local[diff_mask]
        vs_p_local = vs_p_local[diff_mask]

        # Make sure the u is always smaller than the v
        uvs_p = np.sort(np.concatenate((us_p[:, None], vs_p[:, None]), axis=1), axis=1)
        uvs_p_local = np.sort(np.concatenate((us_p_local[:, None], vs_p_local[:, None]), axis=1), axis=1)

        if uvs_p.any():
            edge_ids = find_matching_row_indices_fast(uv_ids, uvs_p)
            edge_ids_local = find_matching_row_indices_fast(uvs_local, uvs_p_local)
        else:
            edge_ids = np.array([])
            edge_ids_local = np.array([])

        return edge_ids, edge_ids_local


    # get the edge features already calculated for the mc-ppl
    edge_features = []
    if 'raw' in feat_list:
        edge_features.append(ds.edge_features(seg_id, 0, anisotropy_factor))
    if 'prob' in feat_list:
        edge_features.append(ds.edge_features(seg_id, 1, anisotropy_factor))
    if 'distance_transform' in feat_list:
        # we assume that the dt was already added as additional input
        edge_features.append(ds.edge_features(seg_id, 2, anisotropy_factor))
    edge_features = np.concatenate(edge_features, axis=1)

    n_paths = np.sum([len(paths) for _, paths in objs_to_paths.iteritems()])

    # Initialize features matrix
    # Factor of three as we use the min, max, and average of the edges at the cutting site
    features = np.zeros((n_paths, edge_features.shape[1] * 3), dtype='float32')

    logger.debug('Starting loops in cut features')

    # 1. ---------------------------
    logger.debug('cut_features, step 1 #####################')
    logger.debug('    Generating local uvs...')

    mappings = {}
    local_uv_masks = {}
    n_vars = {}
    uv_locals = {}

    if ExperimentSettings().n_threads == 1:

        for obj_id in objs_to_paths:

            local_uv_masks[obj_id], mappings[obj_id], uv_locals[obj_id], n_vars[obj_id] = make_local_uv(
                seg, mc_segmentation, uv_ids, obj_id
            )

    else:

        with futures.ThreadPoolExecutor(max_workers=ExperimentSettings().n_threads) as executor:
            tasks = {}
            for obj_id in objs_to_paths:
                tasks[obj_id] = executor.submit(make_local_uv, seg, mc_segmentation, uv_ids, obj_id)
            for obj_id in objs_to_paths:
                local_uv_masks[obj_id], mappings[obj_id], uv_locals[obj_id], n_vars[obj_id] = tasks[obj_id].result()

    # 2. ---------------------------
    logger.debug('cut_features, step 2 #####################')
    logger.debug('    Calculating local graph...')

    weights_locals = {}
    graph_locals = {}

    def get_local_graph(edge_weights, local_uv_mask, n_var, uv_local):
        assert len(local_uv_mask) == len(edge_weights)
        # local weights and graph for the cutter
        weights_local = edge_weights[local_uv_mask]
        graph_local = nifty.graph.UndirectedGraph(n_var)
        graph_local.insertEdges(uv_local)

        return weights_local, graph_local

    for obj_id in objs_to_paths:
        weights_locals[obj_id], graph_locals[obj_id] = get_local_graph(
            edge_weights, local_uv_masks[obj_id], n_vars[obj_id], uv_locals[obj_id]
        )

    # 3. ---------------------------
    logger.debug('cut_features, step 3 #####################')
    logger.debug('   Computing local two-coloring...')

    # FIXME No speedup by parallelization achievable

    local_two_colorings_s = {}
    node_labelings_s = {}

    def get_local_two_coloring(objs_to_path, mapping, graph_local, weights_local):

        # logger.debug('---> for path_id, path in objs_to_paths[obj_id].iteritems(): ...')
        # TODO run graphcut or edge based ws for each path, using end points as seeds
        # determine the cut edge and append to feats
        local_two_colorings = {}
        node_labelings = {}
        for path_id, path in objs_to_path.iteritems():
            # logger.debug('------> path_id = {}'.format(path_id))

            # get source and sink
            # == start and end node of the path
            source = mapping[seg[tuple(path[0])]]
            sink = mapping[seg[tuple(path[-1])]]

            # run cut with seeds at path end points
            # returns the cut edges
            local_two_colorings[path_id], node_labelings[path_id] = cutter(graph_local, weights_local, source, sink)

        return local_two_colorings, node_labelings

    for obj_id in objs_to_paths:
        local_two_colorings_s[obj_id], node_labelings_s[obj_id] = get_local_two_coloring(
            objs_to_paths[obj_id], mappings[obj_id], graph_locals[obj_id], weights_locals[obj_id]
        )

    # # TODO: For debug purposes:
    # # TODO: Restore the segmentation and save it as volume
    # for obj_id in objs_to_paths:
    #     for path_id, path in objs_to_paths[obj_id].iteritems():
    #
    #         if not local_two_colorings_s[obj_id][path_id].max():
    #
    #             # Make global list of node labels initialized to zero
    #             node_labels_global = np.zeros((uv_ids.max() + 1,))
    #             # Set the values of the local nodes to the respective label
    #             reverse_mapping = {val: key for key, val in mappings[obj_id].iteritems()}
    #             node_labels = node_labelings_s[obj_id][path_id].astype('uint32')
    #             for node_label_id, node_label in enumerate(node_labels):
    #                 node_labels_global[reverse_mapping[node_label_id]] = node_label + 1
    #             split_obj_im = ds.project_mc_result(0, node_labels_global)
    #             save_filepath = '/export/home/jhennies/debug/cut_features/{}_{}_{}.h5'.format(ds.ds_name, obj_id, path_id)
    #             vigra.writeHDF5(split_obj_im, save_filepath, 'data', compression='gzip')

    # 4. ---------------------------
    logger.debug('cut_features, step 4 #####################')
    logger.debug('    Compute the edges along the paths...')

    # FIXME This is still the bottleneck, but pretty much ok
    # Parallelization speeds up a little bit

    edge_ids_s = {}
    edge_ids_locals = {}

    if ExperimentSettings().n_threads == 1:

        for obj_id in objs_to_paths:
            for path_id, path in objs_to_paths[obj_id].iteritems():
                edge_ids_s[(obj_id, path_id)], edge_ids_locals[(obj_id, path_id)] = edges_along_path(
                    path, mappings[obj_id], uv_locals[obj_id]
                )

    else:

        with futures.ThreadPoolExecutor(max_workers=ExperimentSettings().n_threads) as executor:
            tasks = {}
            for obj_id in objs_to_paths:
                for path_id, path in objs_to_paths[obj_id].iteritems():
                    tasks[(obj_id, path_id)] = executor.submit(
                        edges_along_path,
                        path, mappings[obj_id], uv_locals[obj_id]
                    )
            for idx, task in tasks.iteritems():
                edge_ids_s[idx], edge_ids_locals[idx] = task.result()

    # 5. ---------------------------
    logger.debug('cut_features, step 5 #####################')
    logger.debug('    Compute edge features...')

    new_edge_feats_s_s = {}

    def get_edge_feats(objs_to_path, local_two_colorings, edge_ids_s, edge_ids_locals):
        new_edge_feats_s = {}

        # logger.debug('---> for path_id, path in objs_to_paths[obj_id].iteritems(): ...')
        # TODO run graphcut or edge based ws for each path, using end points as seeds
        # determine the cut edge and append to feats
        for path_id, path in objs_to_path.iteritems():
            # logger.debug('------> path_id = {}'.format(path_id))

            local_two_coloring = local_two_colorings[path_id]

            edge_ids = edge_ids_s[(obj_id, path_id)]
            edge_ids_local = edge_ids_locals[(obj_id, path_id)]

            # FIXME: This should always be the case, still it happened -> nvestigate!
            # FIXME: Remove this condition once the bug is found and fixed
            # Apparently this happens when a path is very short and starts and ends in the same superpixel
            # TODO: Possible solutions:
            #   1. Generally remove small paths (beware of anisotropy)
            #   2. Keep the condition below
            #   3. Make sure small paths are not computed in the first place
            #       a) Merge close border contacts if they belong to the same object
            #       b) Do not compute a path if it would start and end in the same superpixel
            if not local_two_coloring.max():

                # Probably boundary intersection point calculation failed yielding a path starting
                # and ending in [40, 0, 0] -> corner of the image
                logger.warning('Local two coloring failed.')

            else:

                # find the cut edge along the path
                cut_edges = np.where(local_two_coloring == 1)[0]
                cut_edges_on_path = np.intersect1d(cut_edges, edge_ids_local)

                # TODO: Use average, min, and max
                # # make sure we only have 1 cut edge
                # # assert cut_edge_on_path.size == 1
                # if cut_edges_on_path.size != 1:
                #     print 'Warning: cut_edges_on_path.size = {}'.format(cut_edges_on_path.size)
                # else:
                #     print 'cut_edges_on_path.size = 1'
                # cut_edges_on_path = cut_edges_on_path[0]

                # # cut-edge project back to global edge-indexing and get according features
                # global_edge = edge_ids[edge_ids_local == cut_edges_on_path]
                # features[path_id] = edge_features[global_edge[0], :]

                new_edge_feats = []
                for cut_edge in cut_edges_on_path:
                    global_edge = edge_ids[edge_ids_local == cut_edge]
                    new_edge_feats.append(edge_features[global_edge[0], :])

                new_edge_feats_s[path_id] = np.array(new_edge_feats)

        return new_edge_feats_s

    for obj_id in objs_to_paths:
        new_edge_feats_s_s[obj_id] = get_edge_feats(
            objs_to_paths[obj_id], local_two_colorings_s[obj_id], edge_ids_s, edge_ids_locals
        )

    # 6. ---------------------------
    logger.debug('cut features, step 6 #####################')
    logger.debug('    Concatenating features...')
    for obj_id, new_edge_feats_s in new_edge_feats_s_s.iteritems():
        for path_id, new_edge_feats in new_edge_feats_s.iteritems():

            if new_edge_feats.any():
                features[path_id] = np.concatenate([
                    new_edge_feats.min(axis=0),
                    new_edge_feats.max(axis=0),
                    new_edge_feats.mean(axis=0)
                ], axis=0)

    logger.debug('End of cut features')

    return features


# features based on most likely cut along path (via graphcut)
# return edge features of corresponding cut, depending on feature list
def cut_features_whole_plane(
        ds,
        seg_id,
        mc_segmentation,
        objs_to_paths,  # dict[merge_ids : dict[path_ids : paths]]
        edge_weights,
        anisotropy_factor,
        feat_list=['raw', 'prob', 'distance_transform'],
        cut_method='watershed'
):

    cutters = {
        'watershed': cut_watershed,
        'graphcut': cut_graphcut,
        'seeded_clustering': cut_seeded_agglomeration
    }
    assert feat_list
    assert cut_method in cutters
    cutter = cutters[cut_method]

    seg = ds.seg(seg_id)
    uv_ids = ds.uv_ids(seg_id)

    # find the global and local edge ids along the path, as well as the local
    # start and ed point of the path
    def edges_along_path(path, mapping, uvs_local):

        edge_ids = []
        edge_ids_local = []

        u = seg[tuple(path[0])]
        u_local = mapping[seg[tuple(path[0])]]

        # find edge-ids along the path
        # if this turns out to be a bottleneck, we can c++ it
        for p in path[1:]:

            v = seg[tuple(p)]
            v_local = mapping[seg[tuple(p)]]

            if u != v:
                uv = np.array([[min(u, v), max(u, v)]])
                uv_local = np.array([[min(u_local, v_local), max(u_local, v_local)]])

                edge_id = find_matching_row_indices(uv_ids, uv)[0, 0]
                edge_id_local = find_matching_row_indices(uvs_local, uv_local)[0, 0]

                edge_ids.append(edge_id)
                edge_ids_local.append(edge_id_local)

            u = v
            u_local = v_local

        return np.array(edge_ids), np.array(edge_ids_local)

    # get the edge features already calculated for the mc-ppl
    edge_features = []
    if 'raw' in feat_list:
        edge_features.append(ds.edge_features(seg_id, 0, anisotropy_factor))
    if 'prob' in feat_list:
        edge_features.append(ds.edge_features(seg_id, 1, anisotropy_factor))
    if 'distance_transform' in feat_list:
        # we assume that the dt was already added as additional input
        edge_features.append(ds.edge_features(seg_id, 2, anisotropy_factor))
    edge_features = np.concatenate(edge_features, axis=1)

    n_paths = np.sum([len(paths) for _, paths in objs_to_paths.iteritems()])

    # Initialize features matrix
    # Factor of three as we use the min, max, and average of the edges at the cutting site
    features = np.zeros((n_paths, edge_features.shape[1] * 3), dtype='float32')

    # TODO parallelize
    for obj_id in objs_to_paths:

        local_uv_mask, mapping, reverse_mapping = extract_local_graph_from_segmentation(
            ds,
            seg_id,
            mc_segmentation,
            obj_id,
            uv_ids,
            uv_ids_lifted=None
        )
        uv_local = np.array([[mapping[u] for u in uv] for uv in uv_ids[local_uv_mask]])

        assert len(local_uv_mask) == len(edge_weights)
        # local weights and graph for the cutter
        weights_local = edge_weights[local_uv_mask]
        graph_local = nifty.graph.UndirectedGraph(uv_local.max() + 1)
        graph_local.insertEdges(uv_local)

        # TODO run graphcut or edge based ws for each path, using end points as seeds
        # determine the cut edge and append to feats
        for path_id, path in objs_to_paths[obj_id].iteritems():
            edge_ids, edge_ids_local = edges_along_path(path, mapping, uv_local)

            # get source and sink
            # == start and end node of the path
            source = mapping[seg[tuple(path[0])]]
            sink = mapping[seg[tuple(path[-1])]]

            # run cut with seeds at path end points
            # returns the cut edges
            local_two_coloring = cutter(graph_local, weights_local, source, sink)

            # find the cut edge along the path
            cut_edges = np.where(local_two_coloring == 1)[0]
            # cut_edges_on_path = np.intersect1d(cut_edges, edge_ids_local)

            new_edge_feats = []
            for cut_edge in cut_edges:
                global_edge = edge_ids[edge_ids_local == cut_edge]
                new_edge_feats.append(edge_features[global_edge[0], :])

            new_edge_feats = np.array(new_edge_feats)

            features[path_id] = np.concatenate([
                new_edge_feats.min(axis=0),
                new_edge_feats.max(axis=0),
                new_edge_feats.mean(axis=0)
            ], axis=0)

    return features
