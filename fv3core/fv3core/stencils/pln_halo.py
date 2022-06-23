from gt4py.gtscript import FORWARD, computation, horizontal, interval, region, log

from pace.dsl.typing import FloatField, FloatFieldIJ

def pln_halo(
    pet: FloatFieldIJ, ptop: float, pk3: FloatField, delp: FloatField  
):
    from __externals__ import local_ie, local_is, local_je, local_js
    
    with computation(FORWARD):
        with interval(0,1):
            with horizontal(
                region[local_is - 2 : local_is, local_js : local_je + 1],
                region[local_ie + 1 : local_ie + 3, local_js : local_je + 1],
                region[local_is - 2 : local_ie + 3, local_js - 2 : local_js ],
                region[local_is - 2 : local_ie + 3, local_je + 1, local_je + 3],
            ):
                pet = ptop 
        with interval(1, None):
            with horizontal(
                region[local_is - 2 : local_is, local_js : local_je + 1],
                region[local_ie + 1 : local_ie + 3, local_js : local_je + 1],
                region[local_is - 2 : local_ie + 3, local_js - 2 : local_js ],
                region[local_is - 2 : local_ie + 3, local_je + 1, local_je + 3],
            ):
                pet = pet + delp[0,0,-1]
                pk3 = log(pet)    
    
class PLNHalo:
    """
    Fortran name is pln_halo
    """

    def __init__(self, stencil_factory: StencilFactory):
        grid_indexing = stencil_factory.grid_indexing
        origin = grid_indexing.origin_full()
        domain = grid_indexing.domain_full(add=(0, 0, 1))
        ax_offsets = grid_indexing.axis_offsets(origin, domain)
        self._pln_halo = stencil_factory.from_origin_domain(
            func=pln_halo,
            externals={
                **ax_offsets,
            },
            origin=origin,
            domain=domain,
        )
        shape_2D = grid_indexing.domain_fill(add=(1,1,1))[0:2]
        self._pet_tmp = utils.make_storage_from_shape(
            shape_2D, grid_indexing.origin_full(), backend=stencil_factory.backend
        )

    def __call_(self, pk3: FloatField, delp: FloatField, ptop: float):
        """Update pressure (pk3) in pln halo region

        Args:
            pk3: Interface pressure raised to power of kappa using constant kappa
            delp: Vertical delta in pressure
            ptop: The pressure level at the top of atmosphere

        """
        
        self._pln_halo(self._pet_tmp, delp, pk3, ptop)
        