import dataclasses
from typing import Dict, Optional, Sequence, Union

import dace
from dace.frontend.python.interface import nounroll as dace_nounroll
from gt4py.gtscript import (
    __INLINED,
    BACKWARD,
    FORWARD,
    PARALLEL,
    computation,
    horizontal,
    interval,
    region,
)

import fv3core.stencils.basic_operations as basic
import fv3core.stencils.d_sw as d_sw
import fv3core.stencils.nh_p_grad as nh_p_grad
import fv3core.stencils.pe_halo as pe_halo
import fv3core.stencils.ray_fast as ray_fast
import fv3core.stencils.temperature_adjust as temperature_adjust
import fv3core.stencils.updatedzc as updatedzc
import fv3core.stencils.updatedzd as updatedzd
import pace.dsl.gt4py_utils as utils
import pace.util
import pace.util as fv3util
import pace.util.constants as constants
from fv3core._config import AcousticDynamicsConfig
from fv3core.stencils.c_sw import CGridShallowWaterDynamics
from fv3core.stencils.del2cubed import HyperdiffusionDamping
from fv3core.stencils.pk3_halo import PK3Halo
from fv3core.stencils.riem_solver3 import RiemannSolver3
from fv3core.stencils.riem_solver_c import RiemannSolverC
from pace.dsl.dace.orchestrate import dace_inhibitor
from pace.dsl.stencil import GridIndexing, StencilFactory
from pace.dsl.typing import FloatField, FloatFieldIJ, FloatFieldK
from pace.util import X_DIM, Y_DIM, Z_DIM, Z_INTERFACE_DIM
from pace.util.grid import DampingCoefficients, GridData


HUGE_R = 1.0e40


def zero_data(
    mfxd: FloatField,
    mfyd: FloatField,
    cxd: FloatField,
    cyd: FloatField,
    heat_source: FloatField,
    diss_estd: FloatField,
    first_timestep: bool,
):
    """
    Args:
        mfxd (out):
        mfyd (out):
        cxd (out):
        cyd (out):
        heat_source (out):
        diss_estd (out):
        first_timestep (in):
    """
    with computation(PARALLEL), interval(...):
        mfxd = 0.0
        mfyd = 0.0
        cxd = 0.0
        cyd = 0.0
        if first_timestep:
            with horizontal(region[3:-3, 3:-3]):
                heat_source = 0.0
                diss_estd = 0.0


# NOTE in Fortran these are columns
def dp_ref_compute(
    ak: FloatFieldK,
    bk: FloatFieldK,
    phis: FloatFieldIJ,
    dp_ref: FloatField,
    zs: FloatField,
    rgrav: float,
):
    with computation(PARALLEL), interval(0, -1):
        dp_ref = ak[1] - ak + (bk[1] - bk) * 1.0e5
    with computation(PARALLEL), interval(...):
        zs = phis * rgrav


def set_gz(zs: FloatFieldIJ, delz: FloatField, gz: FloatField):
    with computation(BACKWARD):
        with interval(-1, None):
            gz[0, 0, 0] = zs
        with interval(0, -1):
            gz[0, 0, 0] = gz[0, 0, 1] - delz


def set_pem(delp: FloatField, pem: FloatField, ptop: float):
    with computation(FORWARD):
        with interval(0, 1):
            pem[0, 0, 0] = ptop
        with interval(1, None):
            pem[0, 0, 0] = pem[0, 0, -1] + delp


def compute_geopotential(zh: FloatField, gz: FloatField):
    with computation(PARALLEL), interval(...):
        gz = zh * constants.GRAV


def p_grad_c_stencil(
    rdxc: FloatFieldIJ,
    rdyc: FloatFieldIJ,
    uc: FloatField,
    vc: FloatField,
    delpc: FloatField,
    pkc: FloatField,
    gz: FloatField,
    dt2: float,
):
    """Update C-grid winds from the pressure gradient force

    When this is run the C-grid winds have almost been completely
    updated by computing the momentum equation terms, but the pressure
    gradient force term has not yet been applied. This stencil completes
    the equation and Arakawa C-grid winds have been advected half a timestep
    upon completing this stencil..

     Args:
        rdxc (in):
        rdyc (in):
        uc (inout): x-velocity on the C-grid
        vc (inout): y-velocity on the C-grid
        delpc (in): vertical delta in pressure
        pkc (in):  pressure if non-hydrostatic,
            (edge pressure)**(moist kappa) if hydrostatic
        gz (in):  height of the model grid cells (m)
        dt2 (in): half a model timestep (for C-grid update) in seconds
    """
    from __externals__ import hydrostatic

    with computation(PARALLEL), interval(...):
        if __INLINED(hydrostatic):
            wk = pkc[0, 0, 1] - pkc
        else:
            wk = delpc
        uc = uc + dt2 * rdxc / (wk[-1, 0, 0] + wk) * (
            (gz[-1, 0, 1] - gz) * (pkc[0, 0, 1] - pkc[-1, 0, 0])
            + (gz[-1, 0, 0] - gz[0, 0, 1]) * (pkc[-1, 0, 1] - pkc)
        )

        vc = vc + dt2 * rdyc / (wk[0, -1, 0] + wk) * (
            (gz[0, -1, 1] - gz) * (pkc[0, 0, 1] - pkc[0, -1, 0])
            + (gz[0, -1, 0] - gz[0, 0, 1]) * (pkc[0, -1, 1] - pkc)
        )


def get_nk_heat_dissipation(
    config: d_sw.DGridShallowWaterLagrangianDynamicsConfig, npz: int
) -> int:
    # determines whether to convert dissipated kinetic energy into heat in the full
    # column, not at all, or in 1 or 2 of the top of atmosphere sponge layers
    if config.convert_ke or config.vtdm4 > 1.0e-4:
        nk_heat_dissipation = npz
    else:
        if config.d2_bg_k1 < 1.0e-3:
            nk_heat_dissipation = 0
        else:
            if config.d2_bg_k2 < 1.0e-3:
                nk_heat_dissipation = 1
            else:
                nk_heat_dissipation = 2
    return nk_heat_dissipation


def _quantity_wrap(storage, dims: Sequence[str], grid_indexing: GridIndexing):
    origin, extent = grid_indexing.get_origin_domain(dims)
    return pace.util.Quantity(
        storage,
        dims=dims,
        units="unknown",
        origin=origin,
        extent=extent,
    )


def dyncore_temporaries(grid_indexing: GridIndexing, *, backend: str):
    tmps: Dict[str, Union[pace.util.Quantity, "FloatField"]] = {}
    utils.storage_dict(
        tmps,
        ["ut", "vt", "gz", "zh", "pem", "pkc", "pk3", "heat_source", "divgd"],
        grid_indexing.max_shape,
        grid_indexing.origin_full(),
        backend=backend,
    )
    utils.storage_dict(
        tmps,
        ["ws3"],
        grid_indexing.max_shape[0:2],
        grid_indexing.origin_full()[0:2],
        backend=backend,
    )
    utils.storage_dict(
        tmps,
        ["crx", "xfx"],
        grid_indexing.max_shape,
        grid_indexing.origin_compute(add=(0, -grid_indexing.n_halo, 0)),
        backend=backend,
    )
    utils.storage_dict(
        tmps,
        ["cry", "yfx"],
        grid_indexing.max_shape,
        grid_indexing.origin_compute(add=(-grid_indexing.n_halo, 0, 0)),
        backend=backend,
    )
    tmps["heat_source"] = _quantity_wrap(
        tmps["heat_source"], [X_DIM, Y_DIM, Z_DIM], grid_indexing
    )
    tmps["divgd"] = _quantity_wrap(
        tmps["divgd"],
        dims=[fv3util.X_INTERFACE_DIM, fv3util.Y_INTERFACE_DIM, fv3util.Z_DIM],
        grid_indexing=grid_indexing,
    )
    for name in ["gz", "pkc", "zh"]:
        tmps[name] = _quantity_wrap(
            tmps[name],
            dims=[fv3util.X_DIM, fv3util.Y_DIM, fv3util.Z_INTERFACE_DIM],
            grid_indexing=grid_indexing,
        )

    return tmps


class AcousticDynamics:
    """
    Fortran name is dyn_core
    Peforms the Lagrangian acoustic dynamics described by Lin 2004
    """

    class _WrappedHaloUpdater:
        def __init__(
            self, updater, state, qty_x_names, qty_y_names=None, comm=None
        ) -> None:
            self._updater = updater
            self._state = state
            self._qtx_x_names = qty_x_names
            self._qtx_y_names = qty_y_names
            self._comm = comm

        @dace_inhibitor
        def start(self):
            if self._qtx_y_names is None:
                if dataclasses.is_dataclass(self._state):
                    self._updater.start(
                        [self._state.__getattribute__(x) for x in self._qtx_x_names]
                    )
                elif isinstance(self._state, dict):
                    self._updater.start([self._state[x] for x in self._qtx_x_names])
                else:
                    raise NotImplementedError
            else:
                if dataclasses.is_dataclass(self._state):
                    self._updater.start(
                        [self._state.__getattribute__(x) for x in self._qtx_x_names],
                        [self._state.__getattribute__(y) for y in self._qtx_y_names],
                    )
                elif isinstance(self._state, dict):
                    self._updater.start(
                        [self._state[x] for x in self._qtx_x_names],
                        [self._state[y] for y in self._qtx_y_names],
                    )
                else:
                    raise NotImplementedError

        @dace_inhibitor
        def wait(self):
            self._updater.wait()

        @dace_inhibitor
        def update(self):
            self.start()
            self.wait()

        @dace_inhibitor
        def interface(self):
            assert len(self._qtx_x_names) == 1
            assert len(self._qtx_y_names) == 1
            self._comm.synchronize_vector_interfaces(
                self._state.__getattribute__(self._qtx_x_names[0]),
                self._state.__getattribute__(self._qtx_y_names[0]),
            )

    class _HaloUpdaters(object):
        """Encapsulate all HaloUpdater objects"""

        def __init__(
            self,
            comm: pace.util.CubedSphereCommunicator,
            grid_indexing: GridIndexing,
            backend: str,
            state,
        ):
            origin = grid_indexing.origin_compute()
            shape = grid_indexing.max_shape
            # Define the memory specification required
            # Those can be re-used as they are read-only descriptors
            full_size_xyz_halo_spec = grid_indexing.get_quantity_halo_spec(
                shape,
                origin,
                dims=[fv3util.X_DIM, fv3util.Y_DIM, fv3util.Z_DIM],
                n_halo=grid_indexing.n_halo,
                backend=backend,
            )
            full_size_xyiz_halo_spec = grid_indexing.get_quantity_halo_spec(
                shape,
                origin,
                dims=[fv3util.X_DIM, fv3util.Y_INTERFACE_DIM, fv3util.Z_DIM],
                n_halo=grid_indexing.n_halo,
                backend=backend,
            )
            full_size_xiyz_halo_spec = grid_indexing.get_quantity_halo_spec(
                shape,
                origin,
                dims=[fv3util.X_INTERFACE_DIM, fv3util.Y_DIM, fv3util.Z_DIM],
                n_halo=grid_indexing.n_halo,
                backend=backend,
            )
            full_size_xyzi_halo_spec = grid_indexing.get_quantity_halo_spec(
                shape,
                origin,
                dims=[fv3util.X_DIM, fv3util.Y_DIM, fv3util.Z_INTERFACE_DIM],
                n_halo=grid_indexing.n_halo,
                backend=backend,
            )
            full_size_xiyiz_halo_spec = grid_indexing.get_quantity_halo_spec(
                shape,
                origin,
                dims=[fv3util.X_INTERFACE_DIM, fv3util.Y_INTERFACE_DIM, fv3util.Z_DIM],
                n_halo=grid_indexing.n_halo,
                backend=backend,
            )

            # Build the HaloUpdater. We could build one updater per specification group
            # but because of call overlap between different variable, we kept the
            # straighforward solution of one HaloUpdater per group of updated variable.
            # It also makes the code in call() more readable
            # [DaCe] Wrapping call to a DaCe readable halo updater
            #        Biggest parsing issue is that DaCe cannot do
            #        quantities at runtime paradigm
            self.q_con__cappa = AcousticDynamics._WrappedHaloUpdater(
                comm.get_scalar_halo_updater([full_size_xyz_halo_spec] * 2),
                state,
                ["q_con", "cappa"],
            )
            self.delp__pt = AcousticDynamics._WrappedHaloUpdater(
                comm.get_scalar_halo_updater([full_size_xyz_halo_spec] * 2),
                state,
                ["delp", "pt"],
            )
            self.u__v = AcousticDynamics._WrappedHaloUpdater(
                comm.get_vector_halo_updater(
                    [full_size_xyiz_halo_spec], [full_size_xiyz_halo_spec]
                ),
                state,
                ["u"],
                ["v"],
            )
            self.w = AcousticDynamics._WrappedHaloUpdater(
                comm.get_scalar_halo_updater([full_size_xyz_halo_spec]),
                state,
                ["w"],
            )
            self.gz = AcousticDynamics._WrappedHaloUpdater(
                comm.get_scalar_halo_updater([full_size_xyzi_halo_spec]),
                state,
                ["gz"],
            )
            self.delp__pt__q_con = AcousticDynamics._WrappedHaloUpdater(
                comm.get_scalar_halo_updater([full_size_xyz_halo_spec] * 3),
                state,
                ["delp", "pt", "q_con"],
            )
            self.zh = AcousticDynamics._WrappedHaloUpdater(
                comm.get_scalar_halo_updater([full_size_xyzi_halo_spec]),
                state,
                ["zh"],
            )
            self.divgd = AcousticDynamics._WrappedHaloUpdater(
                comm.get_scalar_halo_updater([full_size_xiyiz_halo_spec]),
                state,
                ["divgd"],
            )
            self.heat_source = AcousticDynamics._WrappedHaloUpdater(
                comm.get_scalar_halo_updater([full_size_xyz_halo_spec]),
                state,
                ["heat_source"],
            )
            if grid_indexing.domain[0] == grid_indexing.domain[1]:
                full_3Dfield_2pts_halo_spec = grid_indexing.get_quantity_halo_spec(
                    shape,
                    origin,
                    dims=[fv3util.X_DIM, fv3util.Y_DIM, fv3util.Z_INTERFACE_DIM],
                    n_halo=2,
                    backend=backend,
                )
                self.pkc = AcousticDynamics._WrappedHaloUpdater(
                    comm.get_scalar_halo_updater([full_3Dfield_2pts_halo_spec]),
                    state,
                    ["pkc"],
                )
            else:
                self.pkc = comm.get_scalar_halo_updater([full_size_xyzi_halo_spec])
            self.uc__vc = AcousticDynamics._WrappedHaloUpdater(
                comm.get_vector_halo_updater(
                    [full_size_xiyz_halo_spec], [full_size_xyiz_halo_spec]
                ),
                state,
                ["uc"],
                ["vc"],
            )
            self.interface_uc__vc = AcousticDynamics._WrappedHaloUpdater(
                None, state, ["u"], ["v"], comm=comm
            )

    def __init__(
        self,
        comm: pace.util.CubedSphereCommunicator,
        stencil_factory: StencilFactory,
        grid_data: GridData,
        damping_coefficients: DampingCoefficients,
        grid_type,
        nested,
        stretched_grid,
        config: AcousticDynamicsConfig,
        pfull: FloatFieldK,
        phis: FloatFieldIJ,
        state,  # [DaCe] hack to get around quantity as parameters for halo updates
        checkpointer: Optional[pace.util.Checkpointer] = None,
    ):
        """
        Args:
            comm: object for cubed sphere inter-process communication
            stencil_factory: creates stencils
            grid_data: metric terms defining the grid
            damping_coefficients: damping configuration
            grid_type: ???
            nested: ???
            stretched_grid: ???
            config: configuration settings
            pfull: atmospheric Eulerian grid reference pressure (Pa)
            phis: surface geopotential height
            checkpointer: if given, used to perform operations on model data
                at specific points in model execution, such as testing against
                reference data
        """
        self.call_checkpointer = checkpointer is not None
        self.checkpointer = checkpointer
        grid_indexing = stencil_factory.grid_indexing
        self.comm = comm
        self.config = config
        assert config.d_ext == 0, "d_ext != 0 is not implemented"
        assert config.beta == 0, "beta != 0 is not implemented"
        assert not config.use_logp, "use_logp=True is not implemented"
        self._da_min = damping_coefficients.da_min
        self.grid_data = grid_data
        self._ptop = self.grid_data.ptop
        self._ks = self.grid_data.ks
        self._pfull = pfull
        self._nk_heat_dissipation = get_nk_heat_dissipation(
            config.d_grid_shallow_water,
            npz=grid_indexing.domain[2],
        )
        self.nonhydrostatic_pressure_gradient = (
            nh_p_grad.NonHydrostaticPressureGradient(
                stencil_factory, grid_data, config.grid_type
            )
        )
        self._temporaries = dyncore_temporaries(
            grid_indexing, backend=stencil_factory.backend
        )
        # This is only here so the temporaries are attributes on this class,
        # to more easily pick them up in unit testing
        # if self._temporaries were a dataclass we can remove this
        for name, value in self._temporaries.items():
            setattr(self, f"_tmp_{name}", value)
        if not config.hydrostatic:
            self._temporaries["pk3"][:] = HUGE_R

        column_namelist = d_sw.get_column_namelist(
            config.d_grid_shallow_water,
            grid_indexing.domain[2],
            backend=stencil_factory.backend,
        )
        if not config.hydrostatic:
            # To write lower dimensional storages, these need to be 3D
            # then converted to lower dimensional
            dp_ref_3d = utils.make_storage_from_shape(
                grid_indexing.max_shape, backend=stencil_factory.backend
            )
            zs_3d = utils.make_storage_from_shape(
                grid_indexing.max_shape, backend=stencil_factory.backend
            )

            dp_ref_stencil = stencil_factory.from_origin_domain(
                dp_ref_compute,
                origin=grid_indexing.origin_full(),
                domain=grid_indexing.domain_full(add=(0, 0, 1)),
            )
            dp_ref_stencil(
                self.grid_data.ak,
                self.grid_data.bk,
                phis,
                dp_ref_3d,
                zs_3d,
                1.0 / constants.GRAV,
            )
            # After writing, make 'dp_ref' a K-field and 'zs' an IJ-field
            self._dp_ref = utils.make_storage_data(
                dp_ref_3d[0, 0, :],
                (dp_ref_3d.shape[2],),
                (0,),
                backend=stencil_factory.backend,
            )
            self._zs = utils.make_storage_data(
                zs_3d[:, :, 0],
                zs_3d.shape[0:2],
                (0, 0),
                backend=stencil_factory.backend,
            )
            self.update_height_on_d_grid = updatedzd.UpdateHeightOnDGrid(
                stencil_factory,
                damping_coefficients,
                grid_data,
                grid_type,
                config.hord_tm,
                self._dp_ref,
                column_namelist,
            )
            self.riem_solver3 = RiemannSolver3(stencil_factory, config.riemann)
            self.riem_solver_c = RiemannSolverC(stencil_factory, p_fac=config.p_fac)
            origin, domain = grid_indexing.get_origin_domain(
                [X_DIM, Y_DIM, Z_INTERFACE_DIM], halos=(2, 2)
            )
            self._compute_geopotential_stencil = stencil_factory.from_origin_domain(
                compute_geopotential,
                origin=origin,
                domain=domain,
            )
        self.dgrid_shallow_water_lagrangian_dynamics = (
            d_sw.DGridShallowWaterLagrangianDynamics(
                stencil_factory,
                grid_data,
                damping_coefficients,
                column_namelist,
                nested,
                stretched_grid,
                config.d_grid_shallow_water,
            )
        )

        self.delpc = utils.make_storage_from_shape(
            grid_indexing.domain_full(add=(1, 1, 1)),
            backend=stencil_factory.backend,
        )
        self.ptc = utils.make_storage_from_shape(
            grid_indexing.domain_full(add=(1, 1, 1)),
            backend=stencil_factory.backend,
        )

        self.cgrid_shallow_water_lagrangian_dynamics = CGridShallowWaterDynamics(
            stencil_factory,
            grid_data,
            nested,
            config.grid_type,
            config.nord,
        )

        self._set_gz = stencil_factory.from_origin_domain(
            set_gz,
            origin=grid_indexing.origin_compute(),
            domain=grid_indexing.domain_compute(add=(0, 0, 1)),
        )
        self._set_pem = stencil_factory.from_origin_domain(
            set_pem,
            origin=grid_indexing.origin_compute(add=(-1, -1, 0)),
            domain=grid_indexing.domain_compute(add=(2, 2, 0)),
        )

        self._p_grad_c = stencil_factory.from_origin_domain(
            p_grad_c_stencil,
            origin=grid_indexing.origin_compute(),
            domain=grid_indexing.domain_compute(add=(1, 1, 0)),
            externals={"hydrostatic": config.hydrostatic},
        )

        self.update_geopotential_height_on_c_grid = (
            updatedzc.UpdateGeopotentialHeightOnCGrid(stencil_factory, grid_data.area)
        )

        self._zero_data = stencil_factory.from_origin_domain(
            zero_data,
            origin=grid_indexing.origin_full(),
            domain=grid_indexing.domain_full(),
        )
        ax_offsets_pe = grid_indexing.axis_offsets(
            grid_indexing.origin_full(),
            grid_indexing.domain_full(add=(0, 0, 1)),
        )
        self._edge_pe_stencil = stencil_factory.from_origin_domain(
            pe_halo.edge_pe,
            origin=grid_indexing.origin_full(),
            domain=grid_indexing.domain_full(add=(0, 0, 1)),
            externals={**ax_offsets_pe},
            skip_passes=("PruneKCacheFills",),
        )
        """The stencil object responsible for updating the interface pressure"""

        self._do_del2cubed = self._nk_heat_dissipation != 0 and config.d_con > 1.0e-5

        if self._do_del2cubed:
            nf_ke = min(3, config.nord + 1)
            self._hyperdiffusion = HyperdiffusionDamping(
                stencil_factory, damping_coefficients, grid_data.rarea, nmax=nf_ke
            )
        if config.rf_fast:
            self._rayleigh_damping = ray_fast.RayleighDamping(
                stencil_factory,
                rf_cutoff=config.rf_cutoff,
                tau=config.tau,
                hydrostatic=config.hydrostatic,
            )
        self._compute_pkz_tempadjust = stencil_factory.from_origin_domain(
            temperature_adjust.compute_pkz_tempadjust,
            origin=grid_indexing.origin_compute(),
            domain=grid_indexing.restrict_vertical(
                nk=self._nk_heat_dissipation
            ).domain_compute(),
        )
        self._pk3_halo = PK3Halo(stencil_factory)
        self._copy_stencil = stencil_factory.from_origin_domain(
            basic.copy_defn,
            origin=grid_indexing.origin_full(),
            domain=grid_indexing.domain_full(add=(0, 0, 1)),
        )

        # Halo updaters
        self._halo_updaters = AcousticDynamics._HaloUpdaters(
            comm, grid_indexing, stencil_factory.backend, state
        )

    def _checkpoint_csw(self, state, tag: str):
        if self.call_checkpointer:
            self.checkpointer(
                f"C_SW-{tag}",
                delpd=state.delp,
                ptd=state.pt,
                ud=state.u,
                vd=state.v,
                wd=state.w,
                ucd=state.uc,
                vcd=state.vc,
                uad=state.ua,
                vad=state.va,
                utd=state.ut,
                vtd=state.vt,
                divgdd=state.divgd,
            )

    def _checkpoint_dsw_in(self, state):
        if self.call_checkpointer:
            self.checkpointer(
                "D_SW-In",
                ucd=state.uc,
                vcd=state.vc,
                wd=state.w,
                delpcd=state.delpc,
                delpd=state.delp,
                ud=state.u,
                vd=state.v,
                ptd=state.pt,
                uad=state.ua,
                vad=state.va,
                zhd=state.zh,
                divgdd=state.divgd,
                xfxd=state.xfx,
                yfxd=state.yfx,
                mfxd=state.mfxd,
                mfyd=state.mfyd,
            )

    def _checkpoint_dsw_out(self, state):
        if self.call_checkpointer:
            self.checkpointer(
                "D_SW-Out",
                ucd=state.uc,
                vcd=state.vc,
                wd=state.w,
                delpcd=state.delpc,
                delpd=state.delp,
                ud=state.u,
                vd=state.v,
                ptd=state.pt,
                uad=state.ua,
                vad=state.va,
                divgdd=state.divgd,
                xfxd=state.xfx,
                yfxd=state.yfx,
                mfxd=state.mfxd,
                mfyd=state.mfyd,
            )

    # TODO: type hint state when it is possible to do so, when it is a static type
    def __call__(
        self,
        state: dace.constant,
        n_map=1,  # [DaCe] replaces state.n_map
        update_temporaries: dace.constant = True,
    ):
        # u, v, w, delz, delp, pt, pe, pk, phis, wsd, omga, ua, va, uc, vc, mfxd,
        # mfyd, cxd, cyd, pkz, peln, q_con, ak, bk, diss_estd, cappa, mdt, n_split,
        # akap, ptop, n_map, comm):
        end_step = n_map == self.config.k_split
        akap = constants.KAPPA
        dt = state.mdt / self.config.n_split
        dt2 = 0.5 * dt
        n_split = self.config.n_split
        # TODO: When the namelist values are set to 0, use these instead:
        # m_split = 1. + abs(dt_atmos)/real(k_split*n_split*abs(p_split))
        # n_split = nint( real(n0split)/real(k_split*abs(p_split)) * stretch_fac + 0.5 )
        # NOTE: In Fortran model the halo update starts happens in fv_dynamics, not here
        self._halo_updaters.q_con__cappa.start()
        self._halo_updaters.delp__pt.start()
        self._halo_updaters.u__v.start()
        self._halo_updaters.q_con__cappa.wait()

        if update_temporaries:
            state.__dict__.update(self._temporaries)

        self._zero_data(
            state.mfxd,
            state.mfyd,
            state.cxd,
            state.cyd,
            state.heat_source,
            state.diss_estd,
            n_map == 1,
        )

        # "acoustic" loop
        # called this because its timestep is usually limited by horizontal sound-wave
        # processes. Note this is often not the limiting factor near the poles, where
        # the speed of the polar night jets can exceed two-thirds of the speed of sound.
        for it in dace_nounroll(range(n_split)):
            # the Lagrangian dynamics have two parts. First we advance the C-grid winds
            # by half a time step (c_sw). Then the C-grid winds are used to define
            # advective fluxes to advance the D-grid prognostic fields a full time step
            # (the rest of the routines).
            #
            # Along-surface flux terms (mass, heat, vertical momentum, vorticity,
            # kinetic energy gradient terms) are evaluated forward-in-time.
            #
            # The pressure gradient force and elastic terms are then evaluated
            # backwards-in-time, to improve stability.
            remap_step = False
            if self.config.breed_vortex_inline or (it == n_split - 1):
                remap_step = True
            if not self.config.hydrostatic:
                self._halo_updaters.w.start()
                if it == 0:
                    self._set_gz(
                        self._zs,
                        state.delz,
                        state.gz,
                    )
                    self._halo_updaters.gz.start()
            if it == 0:
                self._halo_updaters.delp__pt.wait()

            if it == n_split - 1 and end_step:
                if self.config.use_old_omega:
                    self._set_pem(
                        state.delp,
                        state.pem,
                        self._ptop,
                    )

            self._halo_updaters.u__v.wait()
            if not self.config.hydrostatic:
                self._halo_updaters.w.wait()

            # compute the c-grid winds at t + 1/2 timestep
            self._checkpoint_csw(state, tag="In")
            self.delpc, self.ptc = self.cgrid_shallow_water_lagrangian_dynamics(
                state.delp,
                state.pt,
                state.u,
                state.v,
                state.w,
                state.uc,
                state.vc,
                state.ua,
                state.va,
                state.ut,
                state.vt,
                state.divgd,
                state.omga,
                dt2,
            )
            self._checkpoint_csw(state, tag="Out")

            if self.config.nord > 0:
                self._halo_updaters.divgd.start()
            if not self.config.hydrostatic:
                if it == 0:
                    self._halo_updaters.gz.wait()
                    self._copy_stencil(
                        state.gz,
                        state.zh,
                    )
                else:
                    self._copy_stencil(
                        state.zh,
                        state.gz,
                    )
            if not self.config.hydrostatic:
                self.update_geopotential_height_on_c_grid(
                    self._dp_ref, self._zs, state.ut, state.vt, state.gz, state.ws3, dt2
                )
                self.riem_solver_c(
                    dt2,
                    state.cappa,
                    self._ptop,
                    state.phis,
                    state.ws3,
                    self.ptc,
                    state.q_con,
                    self.delpc,
                    state.gz,
                    state.pkc,
                    state.omga,
                )

            self._p_grad_c(
                self.grid_data.rdxc,
                self.grid_data.rdyc,
                state.uc,
                state.vc,
                self.delpc,
                state.pkc,
                state.gz,
                dt2,
            )
            self._halo_updaters.uc__vc.start()
            if self.config.nord > 0:
                self._halo_updaters.divgd.wait()
            self._halo_updaters.uc__vc.wait()
            # use the computed c-grid winds to evolve the d-grid winds forward
            # by 1 timestep
            self._checkpoint_dsw_in(state)
            self.dgrid_shallow_water_lagrangian_dynamics(
                state.vt,
                state.delp,
                state.pt,
                state.u,
                state.v,
                state.w,
                state.uc,
                state.vc,
                state.ua,
                state.va,
                state.divgd,
                state.mfxd,
                state.mfyd,
                state.cxd,
                state.cyd,
                state.crx,
                state.cry,
                state.xfx,
                state.yfx,
                state.q_con,
                state.zh,
                state.heat_source,
                state.diss_estd,
                dt,
            )
            self._checkpoint_dsw_out(state)
            # note that uc and vc are not needed at all past this point.
            # they will be re-computed from scratch on the next acoustic timestep.

            self._halo_updaters.delp__pt__q_con.update()

            # Not used unless we implement other betas and alternatives to nh_p_grad
            # if self.namelist.d_ext > 0:
            #    raise 'Unimplemented namelist option d_ext > 0'

            if not self.config.hydrostatic:
                # without explicit arg names, numpy does not run
                self.update_height_on_d_grid(
                    surface_height=self._zs,
                    height=state.zh,
                    courant_number_x=state.crx,
                    courant_number_y=state.cry,
                    x_area_flux=state.xfx,
                    y_area_flux=state.yfx,
                    ws=state.wsd,
                    dt=dt,
                )
                self.riem_solver3(
                    remap_step,
                    dt,
                    state.cappa,
                    self._ptop,
                    self._zs,
                    state.wsd,
                    state.delz,
                    state.q_con,
                    state.delp,
                    state.pt,
                    state.zh,
                    state.pe,
                    state.pkc,
                    state.pk3,
                    state.pk,
                    state.peln,
                    state.w,
                )

                self._halo_updaters.zh.start()
                self._halo_updaters.pkc.start()
                if remap_step:
                    self._edge_pe_stencil(state.pe, state.delp, self._ptop)
                if self.config.use_logp:
                    raise NotImplementedError(
                        "unimplemented namelist option use_logp=True"
                    )
                else:
                    self._pk3_halo(state.pk3, state.delp, self._ptop, akap)
            if not self.config.hydrostatic:
                self._halo_updaters.zh.wait()
                self._compute_geopotential_stencil(
                    state.zh,
                    state.gz,
                )
                self._halo_updaters.pkc.wait()

                self.nonhydrostatic_pressure_gradient(
                    state.u,
                    state.v,
                    state.pkc,
                    state.gz,
                    state.pk3,
                    state.delp,
                    dt,
                    self._ptop,
                    akap,
                )

            if self.config.rf_fast:
                # TODO: Pass through ks, or remove, inconsistent representation vs
                # Fortran.
                self._rayleigh_damping(
                    state.u,
                    state.v,
                    state.w,
                    self._dp_ref,
                    self._pfull,
                    dt,
                    self._ptop,
                    self._ks,
                )

            if it != n_split - 1:
                # [DaCe] this should be a reuse of
                #        self._halo_updaters.u__v but it creates
                #        parameter generation issues, and therefore has been duplicated
                self._halo_updaters.u__v.start()
            else:
                if self.config.grid_type < 4:
                    self._halo_updaters.interface_uc__vc.interface()

        if self._do_del2cubed:
            self._halo_updaters.heat_source.update()
            # TODO: move dependence on da_min into init of hyperdiffusion class
            cd = constants.CNST_0P20 * self._da_min
            self._hyperdiffusion(state.heat_source, cd)
            if not self.config.hydrostatic:
                delt_time_factor = abs(dt * self.config.delt_max)
                self._compute_pkz_tempadjust(
                    state.delp,
                    state.delz,
                    state.cappa,
                    state.heat_source,
                    state.pt,
                    state.pkz,
                    delt_time_factor,
                )
