import dace


def strip_unused_global_in_compute_x_flux(sdfg: dace.SDFG):
    """Remove compute_x_flux al & ar variables transient representations that are
    considered as GPU_Global when they are actually transients to the Tasklet
    """
    for node, state in sdfg.all_nodes_recursive():
        if isinstance(node, dace.nodes.AccessNode) and (
            "al__" in node.data or "ar__" in node.data
        ):
            for e in state.all_edges(node):
                tasklet = None
                if isinstance(state.memlet_path(e)[0].src, dace.nodes.Tasklet):
                    conn = state.memlet_path(e)[0].src_conn
                    tasklet = state.memlet_path(e)[0].src
                elif isinstance(state.memlet_path(e)[-1].dst, dace.nodes.Tasklet):
                    conn = state.memlet_path(e)[-1].dst_conn
                    tasklet = state.memlet_path(e)[-1].dst
                if tasklet is not None:
                    code_str = tasklet.code.as_string
                    dtype = state.parent.arrays[e.data.data].dtype
                    code_str = f"{conn}: dace.{dtype.to_string()}\n" + code_str
                    tasklet.code.as_string = code_str
                state.remove_memlet_path(e, True)


def splittable_region_expansion(sdfg: dace.SDFG):
    """
    Set certain StencilComputation library nodes to expand to a different
    schedule if they contain small splittable regions.
    """
    from gtc.dace.nodes import StencilComputation

    for node, _ in sdfg.all_nodes_recursive():
        if isinstance(node, StencilComputation):
            if node.has_splittable_regions() and "corner" in node.label:
                node.expansion_specification = [
                    "Sections",
                    "Stages",
                    "J",
                    "I",
                    "K",
                ]
                print("Reordered schedule for", node.label)
