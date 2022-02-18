import abc
import dataclasses
from typing import Optional, Sequence

import gt4py.gtscript as gtscript
from gt4py.gtscript import PARALLEL, computation, horizontal, interval, region

import pace.dsl.gt4py_utils as utils
import pace.stencils.corners as corners
from fv3core.stencils.delnflux import DelnFlux
from fv3core.stencils.xppm import XPiecewiseParabolic, compute_x_flux_interior
from fv3core.stencils.yppm import YPiecewiseParabolic, compute_y_flux_interior
from pace.dsl.stencil import StencilFactory
from pace.dsl.typing import FloatField, FloatFieldIJ
from pace.util.grid import DampingCoefficients, GridData


@gtscript.function
def apply_x_flux_divergence(q: FloatField, q_x_flux: FloatField) -> FloatField:
    """
    Update a scalar q according to its flux in the x direction.
    """
    return q + q_x_flux - q_x_flux[1, 0, 0]


@gtscript.function
def apply_y_flux_divergence(q: FloatField, q_y_flux: FloatField) -> FloatField:
    """
    Update a scalar q according to its flux in the x direction.
    """
    return q + q_y_flux - q_y_flux[0, 1, 0]


def finite_volume_transport_interior_stencil(
    q_x_differentiable: FloatField,
    q_y_differentiable: FloatField,
    courant_x: FloatField,
    courant_y: FloatField,
    area: FloatFieldIJ,
    x_area_flux: FloatField,
    y_area_flux: FloatField,
    x_unit_flux: FloatField,
    y_unit_flux: FloatField,
    x_flux: FloatField,
    y_flux: FloatField,
):
    """
    Args:
        q (in):
        area (in):
        y_area_flux (in):
        q_flux_y (out):
        q_i (out):
        q_i_flux_x (out):
    """
    with computation(PARALLEL), interval(...):
        x_flux, y_flux = finite_volume_transport_interior(
            q_x_differentiable=q_x_differentiable,
            q_y_differentiable=q_y_differentiable,
            courant_x=courant_x,
            courant_y=courant_y,
            area=area,
            x_area_flux=x_area_flux,
            y_area_flux=y_area_flux,
            x_unit_flux=x_unit_flux,
            y_unit_flux=y_unit_flux,
        )


@gtscript.function
def finite_volume_transport_interior(
    q_x_differentiable: FloatField,
    q_y_differentiable: FloatField,
    courant_x: FloatField,
    courant_y: FloatField,
    area: FloatFieldIJ,
    x_area_flux: FloatField,
    y_area_flux: FloatField,
    x_unit_flux: FloatField,
    y_unit_flux: FloatField,
):
    q_y_advected_mean = compute_y_flux_interior(q_y_differentiable, courant_y)
    fyy = y_area_flux * q_y_advected_mean
    # note the units of area cancel out, because area is present in all
    # terms in the numerator and denominator of q_i
    # corresponds to FV3 documentation eq 4.18, q_i = f(q)
    q_advected_y = (q_y_differentiable * area + fyy - fyy[0, 1, 0]) / (
        area + y_area_flux - y_area_flux[0, 1, 0]
    )
    q_advected_y_x_advected_mean = compute_x_flux_interior(q_advected_y, courant_x)

    q_x_advected_mean = compute_x_flux_interior(q_x_differentiable, courant_x)
    fxx = x_area_flux * q_x_advected_mean
    area_with_x_flux = apply_x_flux_divergence(area, x_area_flux)
    q_advected_x = (q_x_differentiable * area + fxx - fxx[1, 0, 0]) / area_with_x_flux
    q_advected_x_y_advected_mean = compute_y_flux_interior(q_advected_x, courant_y)
    with horizontal(region[:, :-1]):
        x_flux = 0.5 * (q_advected_y_x_advected_mean + q_x_advected_mean) * x_unit_flux
    with horizontal(region[:-1, :]):
        y_flux = 0.5 * (q_advected_x_y_advected_mean + q_y_advected_mean) * y_unit_flux
    return x_flux, y_flux


def q_i_stencil(
    q: FloatField,
    area: FloatFieldIJ,
    y_area_flux: FloatField,
    q_advected_along_y: FloatField,
    q_i: FloatField,
):
    """
    Args:
        q (in):
        area (in):
        y_area_flux (in):
        q_advected_along_y (in):
        q_i (out):
    """
    with computation(PARALLEL), interval(...):
        fyy = y_area_flux * q_advected_along_y
        # note the units of area cancel out, because area is present in all
        # terms in the numerator and denominator of q_i
        # corresponds to FV3 documentation eq 4.18, q_i = f(q)
        q_i = (q * area + fyy - fyy[0, 1, 0]) / (
            area + y_area_flux - y_area_flux[0, 1, 0]
        )
        # fyy = y_area_flux * q_y_advected_mean
        # # note the units of area cancel out, because area is present in all
        # # terms in the numerator and denominator of q_i
        # # corresponds to FV3 documentation eq 4.18, q_i = f(q)
        # q_advected_y = (q_y_differentiable * area + fyy - fyy[0, 1, 0]) / (
        #     area + y_area_flux - y_area_flux[0, 1, 0]
        # )


def q_j_stencil(
    q: FloatField,
    area: FloatFieldIJ,
    x_area_flux: FloatField,
    fx2: FloatField,
    q_j: FloatField,
):
    """
    Args:
        q (in):
        area (in):
        x_area_flux (in):
        fx2 (in):
        q_j (out):
    """
    with computation(PARALLEL), interval(...):
        fx1 = x_area_flux * fx2
        area_with_x_flux = apply_x_flux_divergence(area, x_area_flux)
        q_j = (q * area + fx1 - fx1[1, 0, 0]) / area_with_x_flux
    # fxx = x_area_flux * q_x_advected_mean
    # area_with_x_flux = apply_x_flux_divergence(area, x_area_flux)
    # q_advected_x = (
    #     q_x_differentiable * area + fxx - fxx[1, 0, 0]
    # ) / area_with_x_flux


def final_fluxes(
    q_advected_y_x_advected_mean: FloatField,
    q_x_advected_mean: FloatField,
    q_advected_x_y_advected_mean: FloatField,
    q_y_advected_mean: FloatField,
    x_unit_flux: FloatField,
    y_unit_flux: FloatField,
    x_flux: FloatField,
    y_flux: FloatField,
):
    """
    Compute final x and y fluxes of q from different numerical representations.

    Corresponds roughly to eq. 4.17 of FV3 documentation, except that the flux
    is in units of q rather than in units of q per interface area per time.
    This corresponds to eq 4.17 with both sides multiplied by
    e.g. x_unit_flux / u^* (similarly for y/v).

    Combining the advection operators in this way is done to cancel leading-order
    numerical splitting error.

    Args:
        q_advected_y_x_advected_mean (in):
        q_x_advected_mean (in):
        q_advected_x_y_advected_mean (in):
        q_y_advected_mean (in):
        x_unit_flux (in):
        y_unit_flux (in):
        x_flux (out):
        y_flux (out):
    """
    with computation(PARALLEL), interval(...):
        with horizontal(region[:, :-1]):
            x_flux = (
                0.5 * (q_advected_y_x_advected_mean + q_x_advected_mean) * x_unit_flux
            )
        with horizontal(region[:-1, :]):
            y_flux = (
                0.5 * (q_advected_x_y_advected_mean + q_y_advected_mean) * y_unit_flux
            )


@dataclasses.dataclass
class CopiedCorners:
    """
    Data container for storages with corners copied for differencing
    along the x- and y-directions.

    Attributes:
        base: writeable version of storage with no guarantees about corner data
        x_differentiable: read-only version of storage which can be differenced
            along the x-direction
        y_differentiable: read-only version of storage which can be differenced
            along the y-direction
    """

    base: FloatField
    x_differentiable: FloatField
    y_differentiable: FloatField


class PreAllocatedCopiedCornersFactory:
    """
    Creates CopiedCorners from a field, using an init-compiled stencil
    and pre-allocated output fields.
    """

    def __init__(
        self,
        stencil_factory: StencilFactory,
        *,
        dims: Sequence[str],
        y_temporary: FloatField,
    ):
        """
        Args:
            stencil_factory: creates stencils
            dims: dimensionality of data to be copied
            y_temporary: if given, storage to use for y-differenceable field
                (x-differenceable field uses same memory as base storage),
                if None then a storage is initialized based on max shape
        """
        if y_temporary is None:
            y_temporary = utils.make_storage_from_shape(
                stencil_factory.grid_indexing.max_shape,
                origin=stencil_factory.grid_indexing.origin_compute(),
                backend=stencil_factory.backend,
            )
        self._copy_corners_xy = corners.CopyCornersXY(
            stencil_factory, dims, y_field=y_temporary
        )

    def __call__(self, field: FloatFieldIJ) -> CopiedCorners:
        x_field, y_field = self._copy_corners_xy(field)
        return CopiedCorners(
            base=field, x_differentiable=x_field, y_differentiable=y_field
        )


class FiniteVolumeTransport:
    """
    Equivalent of Fortran FV3 subroutine fv_tp_2d, done in 3 dimensions.
    Tested on serialized data with FvTp2d
    ONLY USE_SG=False compiler flag implements
    """

    def __init__(
        self,
        stencil_factory: StencilFactory,
        grid_data: GridData,
        damping_coefficients: DampingCoefficients,
        grid_type: int,
        hord,
        nord=None,
        damp_c=None,
    ):
        if stencil_factory.grid_indexing.tile_interior:
            self._stencils: _FiniteVolumeTransportStencils = (
                _FiniteVolumeTransportInteriorStencils(
                    stencil_factory, area=grid_data.area, hord=hord
                )
            )
        else:
            self._stencils = _FiniteVolumeTransportEdgeStencils(
                stencil_factory, grid_data=grid_data, hord=hord
            )
        self._area = grid_data.area

        self._nord = nord
        self._damp_c = damp_c
        if (self._nord is not None) and (self._damp_c is not None):
            self.delnflux: Optional[DelnFlux] = DelnFlux(
                stencil_factory=stencil_factory,
                damping_coefficients=damping_coefficients,
                rarea=grid_data.rarea,
                nord=self._nord,
                damp_c=self._damp_c,
            )
        else:
            self.delnflux = None

    def __call__(
        self,
        q: CopiedCorners,
        crx,
        cry,
        x_area_flux,
        y_area_flux,
        q_x_flux,
        q_y_flux,
        x_mass_flux=None,
        y_mass_flux=None,
        mass=None,
    ):
        """
        Calculate fluxes for horizontal finite volume transport.
        Defined in Putman and Lin 2007 (PL07). Corresponds to equation 4.17
        in the FV3 documentation.
        Divergence terms are handled by advecting the weighting used in
        the units of the scalar, and dividing by its divergence. For example,
        temperature (pt in the Fortran) and tracers are mass weighted, so
        the final tendency is
        e.g. (convergence of tracer) / (convergence of gridcell mass). This
        is described in eq 17 of PL07. pressure thickness and vorticity
        by contrast are area weighted.
        Args:
            q (in): scalar to be transported
            crx (in): Courant number in x-direction
            cry (in): Courant number in y-direction
            x_area_flux (in): flux of area in x-direction, in units of m^2
            y_area_flux (in): flux of area in y-direction, in units of m^2
            q_x_flux (out): transport flux of q in x-direction in units q * m^2,
                corresponding to X in eq 4.17 of FV3 documentation
            q_y_flux (out): transport flux of q in y-direction in units q * m^2,
                corresponding to Y in eq 4.17 of FV3 documentation
            x_mass_flux (in): mass flux in x-direction,
                corresponds to F(rho^* = 1) in PL07 eq 17, if not given
                then q is assumed to have per-area units
            y_mass_flux (in): mass flux in x-direction,
                corresponds to G(rho^* = 1) in PL07 eq 18, if not given
                then q is assumed to have per-area units
            mass (in): ??? passed along to damping code, if scalar is per-mass
                (as opposed to per-area) then this must be provided for
                damping to be correct
        """
        if (
            self.delnflux is not None
            and mass is None
            and (x_mass_flux is not None or y_mass_flux is not None)
        ):
            raise ValueError(
                "when damping is enabled, mass must be given if mass flux is given"
            )
        if x_mass_flux is None:
            x_unit_flux = x_area_flux
        else:
            x_unit_flux = x_mass_flux
        if y_mass_flux is None:
            y_unit_flux = y_area_flux
        else:
            y_unit_flux = y_mass_flux

        self._stencils.call_stencils(
            q=q,
            crx=crx,
            cry=cry,
            x_area_flux=x_area_flux,
            y_area_flux=y_area_flux,
            q_x_flux=q_x_flux,
            q_y_flux=q_y_flux,
            x_unit_flux=x_unit_flux,
            y_unit_flux=y_unit_flux,
        )

        if self.delnflux is not None:
            self.delnflux(q.base, q_x_flux, q_y_flux, mass=mass)


class _FiniteVolumeTransportStencils(abc.ABC):
    """
    Contains just the stencils component of FiniteVolumeTransport classes.
    """

    @abc.abstractmethod
    def call_stencils(
        self,
        q: CopiedCorners,
        crx,
        cry,
        x_area_flux,
        y_area_flux,
        q_x_flux,
        q_y_flux,
        x_unit_flux,
        y_unit_flux,
    ):
        pass


class _FiniteVolumeTransportEdgeStencils(_FiniteVolumeTransportStencils):
    """
    This version works on any rank, including ranks which border a tile edge.
    """

    def __init__(self, stencil_factory: StencilFactory, grid_data: GridData, hord: int):
        # use a shorter alias for grid_indexing here to avoid very verbose lines
        idx = stencil_factory.grid_indexing
        self._area = grid_data.area
        origin = idx.origin_compute()

        def make_storage():
            return utils.make_storage_from_shape(
                idx.max_shape, origin=origin, backend=stencil_factory.backend
            )

        self._q_advected_y = make_storage()
        self._q_advected_x = make_storage()
        self._q_x_advected_mean = make_storage()
        self._q_y_advected_mean = make_storage()
        self._q_advected_x_y_advected_mean = make_storage()
        self._q_advected_y_x_advected_mean = make_storage()
        ord_outer = hord
        ord_inner = 8 if hord == 10 else hord
        self.y_piecewise_parabolic_inner = YPiecewiseParabolic(
            stencil_factory=stencil_factory,
            dya=grid_data.dya,
            jord=ord_inner,
            origin=idx.origin_compute(add=(-idx.n_halo, 0, 0)),
            domain=idx.domain_compute(add=(1 + 2 * idx.n_halo, 1, 1)),
        )
        self.q_i_stencil = stencil_factory.from_origin_domain(
            q_i_stencil,
            origin=idx.origin_full(add=(0, 3, 0)),
            domain=idx.domain_full(add=(0, -3, 1)),
        )
        self.x_piecewise_parabolic_outer = XPiecewiseParabolic(
            stencil_factory=stencil_factory,
            dxa=grid_data.dxa,
            iord=ord_outer,
            origin=idx.origin_compute(),
            domain=idx.domain_compute(add=(1, 1, 1)),
        )
        self.x_piecewise_parabolic_inner = XPiecewiseParabolic(
            stencil_factory=stencil_factory,
            dxa=grid_data.dxa,
            iord=ord_inner,
            origin=idx.origin_compute(add=(0, -idx.n_halo, 0)),
            domain=idx.domain_compute(add=(1, 1 + 2 * idx.n_halo, 1)),
        )
        self.q_j_stencil = stencil_factory.from_origin_domain(
            q_j_stencil,
            origin=idx.origin_full(add=(3, 0, 0)),
            domain=idx.domain_full(add=(-3, 0, 1)),
        )
        self.y_piecewise_parabolic_outer = YPiecewiseParabolic(
            stencil_factory=stencil_factory,
            dya=grid_data.dya,
            jord=ord_outer,
            origin=idx.origin_compute(),
            domain=idx.domain_compute(add=(1, 1, 1)),
        )
        self.stencil_transport_flux = stencil_factory.from_origin_domain(
            final_fluxes,
            origin=idx.origin_compute(),
            domain=idx.domain_compute(add=(1, 1, 1)),
        )

    def call_stencils(
        self,
        q: CopiedCorners,
        crx,
        cry,
        x_area_flux,
        y_area_flux,
        q_x_flux,
        q_y_flux,
        x_unit_flux,
        y_unit_flux,
    ):
        self.y_piecewise_parabolic_inner(
            q.y_differentiable, cry, self._q_y_advected_mean
        )
        # q_y_advected_mean is 1/Delta_area * curly-F, where curly-F is defined in
        # equation 4.3 of the FV3 documentation and Delta_area is the advected area
        # (y_area_flux)
        self.q_i_stencil(
            q.y_differentiable,
            self._area,
            y_area_flux,
            self._q_y_advected_mean,
            self._q_advected_y,
        )  # q_advected_y out is f(q) in eq 4.18 of FV3 documentation
        self.x_piecewise_parabolic_outer(
            self._q_advected_y, crx, self._q_advected_y_x_advected_mean
        )
        # q_advected_y_x_advected_mean is now rho^n + F(rho^y) in PL07 eq 16

        # similarly below for x<->y
        self.x_piecewise_parabolic_inner(
            q.x_differentiable, crx, self._q_x_advected_mean
        )
        self.q_j_stencil(
            q.x_differentiable,
            self._area,
            x_area_flux,
            self._q_x_advected_mean,
            self._q_advected_x,
        )
        self.y_piecewise_parabolic_outer(
            self._q_advected_x, cry, self._q_advected_x_y_advected_mean
        )

        self.stencil_transport_flux(
            self._q_advected_y_x_advected_mean,
            self._q_x_advected_mean,
            self._q_advected_x_y_advected_mean,
            self._q_y_advected_mean,
            x_unit_flux,
            y_unit_flux,
            q_x_flux,
            q_y_flux,
        )


class _FiniteVolumeTransportInteriorStencils(_FiniteVolumeTransportStencils):
    """
    This version works only on ranks in the interior of a tile, not ranks which
    border a tile edge.
    """

    def __init__(
        self,
        stencil_factory: StencilFactory,
        area: FloatFieldIJ,
        hord: int,
    ):
        idx = stencil_factory.grid_indexing
        self._area = area
        ord_outer = hord
        assert hord == 8  # JM: have to update externals
        ord_inner = 8 if hord == 10 else hord
        origin = idx.origin_compute()
        domain = idx.domain_compute(add=(1, 1, 1))
        ax_offsets = stencil_factory.grid_indexing.axis_offsets(origin, domain)
        self._stencil = stencil_factory.from_origin_domain(
            finite_volume_transport_interior_stencil,
            origin=origin,
            domain=domain,
            externals={
                "jord": ord_inner,
                "iord": ord_outer,
                "mord": abs(ord_inner),
                "yt_minmax": True,
                **ax_offsets,
            },
        )

    def call_stencils(
        self,
        q: CopiedCorners,
        crx,
        cry,
        x_area_flux,
        y_area_flux,
        q_x_flux,
        q_y_flux,
        x_unit_flux,
        y_unit_flux,
    ):
        self._stencil(
            q.x_differentiable,
            q.y_differentiable,
            crx,
            cry,
            self._area,
            x_area_flux,
            y_area_flux,
            x_unit_flux,
            y_unit_flux,
            q_x_flux,
            q_y_flux,
        )


# Notes on usages of fvtp2d, to consider fusing this code with the code
# that actually updates the value being advected:

# tracer advection:
# def adjustment(q, dp1, fx, fy, rarea, dp2):
#     return (q * dp1 + (fx - fx[1, 0, 0] + fy - fy[0, 1, 0]) * rarea) / dp2

# height (updatedzd)
# # described in Putman and Lin 2007 equation 7
# # updated area is used because of implicit-in-time evaluation
# area_after_flux = (
#     (area + x_area_flux - x_area_flux[1, 0, 0])
#     + (area + y_area_flux - y_area_flux[0, 1, 0])
#     - area
# )
# # final height is the original volume plus the fluxed volumes,
# # divided by the final area
# return (
#     height * area
#     + x_height_flux
#     - x_height_flux[1, 0, 0]
#     + y_height_flux
#     - y_height_flux[0, 1, 0]
# ) / area_after_flux

# pressure/mass flux gets added to a running total, so it can be evenly applied
# during tracer advection

# flux_adjust for some variables in d_sw:
# with computation(PARALLEL), interval(...):
#     # in the original Fortran, this uses `w` instead of `q`
#     q = q * delp + flux_increment(gx, gy, rarea)
# def flux_increment(gx, gy, rarea):
#     return (gx - gx[1, 0, 0] + gy - gy[0, 1, 0]) * rarea

# apply_pt_delp_fluxes in d_sw:

# if __INLINED(inline_q == 0):
#     with horizontal(region[local_is : local_ie + 1, local_js : local_je + 1]):
#         pt = pt * delp + flux_increment(pt_x_flux, pt_y_flux, rarea)
#         delp = delp + flux_increment(delp_x_flux, delp_y_flux, rarea)
#         pt = pt / delp

# something fairly spread out is done for u and v in d_sw, but it follows
# a general pattern of delnflux then adding fluxes - needs more investigation
