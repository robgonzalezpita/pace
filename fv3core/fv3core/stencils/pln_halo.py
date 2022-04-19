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
    