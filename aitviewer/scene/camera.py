"""
Copyright (C) 2022  ETH Zurich, Manuel Kaufmann, Velko Vechev

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
from abc import ABC, abstractmethod
import numpy as np
import joblib
import os

from aitviewer.configuration import CONFIG as C
from aitviewer.renderables.lines import Lines
from aitviewer.renderables.meshes import Meshes
from aitviewer.renderables.rigid_bodies import RigidBodies
from aitviewer.scene.camera_utils import look_at
from aitviewer.scene.camera_utils import orthographic_projection
from aitviewer.scene.camera_utils import perspective_projection
from aitviewer.scene.node import Node
from trimesh.transformations import rotation_matrix

from aitviewer.utils.decorators import hooked


def _transform_vector(transform, vector):
    """Apply affine transformation (4-by-4 matrix) to a 3D vector."""
    return (transform @ np.concatenate([vector, np.array([1])]))[:3]


class CameraInterface(ABC):
    """
    An abstract class which describes the interface expected by the viewer for using this object as a camera
    """

    @abstractmethod
    def update_matrices(self, width, height):
        """
        Update the matrices of this camera, should always be called before using any of the get_*_matrix() methods.
        """
        pass

    @abstractmethod
    def get_projection_matrix(self):
        """
        Returns the matrix that projects 3D coordinates in camera space to the image plane.
        """
        pass
    
    @abstractmethod
    def get_view_matrix(self):
        """
        Returns the matrix that projects 3D coordinates in camera space to the image plane.
        """
        pass

    @abstractmethod
    def get_view_projection_matrix(self):
        """
        Returns the view-projection matrix, i.e. the 4x4 matrix that maps from homogenous world coordinates to image
        space.
        """
        pass

    @property
    @abstractmethod
    def position(self):
        pass

    @property
    @abstractmethod
    def forward(self):
        pass

    @property
    @abstractmethod
    def up(self):
        pass
    
    @property
    @abstractmethod
    def right(self):
        pass
    
    def gui(self):
        pass


class Camera(Node, CameraInterface):
    """ 
    A base camera object that provides rendering of a camera mesh and visualization of the camera frustum and coordinate system.
    Subclasses of this class must implement the CameraInterface abstract methods.
    """
    def __init__(self, inactive_color=(0.5, 0.5, 0.5, 1), active_color=(0.6, 0.1, 0.1, 1), viewer=None, **kwargs):
        """ Initializer
        :param inactive_color: Color that will be used for rendering this object when inactive
        :param active_color:   Color that will be used for rendering this object when active
        :param viewer: The current viewer, if not None the gui for this object will show a button for viewing from this camera in the viewer
        """

        super(Camera, self).__init__(icon='\u0084', **kwargs)

        # Camera object geometry
        vertices = np.array([
            # Body
            [ 0,  0, 0],
            [-1, -1, 1],
            [-1,  1, 1],
            [ 1, -1, 1],
            [ 1,  1, 1],
            
            # Triangle
            [ 0.5,  1.1, 1],
            [-0.5,  1.1, 1],
            [   0,    2, 1],
        ], dtype=np.float32)

        # Scale dimensions
        vertices[:, 0] *= 0.05
        vertices[:, 1] *= 0.03
        vertices[:, 2] *= 0.15

        # Slide such that the origin is in front of the object
        vertices[:, 2] -= vertices[1, 2] * 1.1

        # Reverse z since we use the opengl convention that camera forward is -z
        vertices[:, 2] *= -1

        # Reverse x too to maintain a consistent triangle winding
        vertices[:, 0] *= -1

        faces = np.array([
            [ 0, 1, 2],
            [ 0, 2, 4],
            [ 0, 4, 3],
            [ 0, 3, 1],
            [ 1, 3, 2],
            [ 4, 2, 3],
            [ 5, 6, 7],
            [ 5, 7, 6],
        ])
        
        self._active = False
        self.active_color = active_color
        self.inactive_color = inactive_color

        self.mesh = Meshes(vertices, faces, cast_shadow=False, flat_shading=True, position=kwargs.get('position'), rotation=kwargs.get('rotation'))
        self.mesh.color = self.inactive_color
        self.add(self.mesh, show_in_hierarchy=False)

        self.frustum = None
        self.origin = None

        self.viewer = viewer
    
    @property
    def active(self):
        return self._active
    
    @active.setter
    def active(self, active):
        self._active = active

        if active:
            self.mesh.color = self.active_color
        else:
            self.mesh.color = self.inactive_color
    
    def hide_frustum(self):
        if self.frustum:
            self.remove(self.frustum)
            self.frustum = None

        if self.origin:
            self.remove(self.origin)
            self.origin = None

    def show_frustum(self, width, height, distance):
        # Remove previous frustum if it exists
        self.hide_frustum()
        
        # Compute lines for each frame
        all_lines = np.zeros((self.n_frames, 24, 3), dtype=np.float32)
        frame_id = self.current_frame_id
        for i in range(self.n_frames):
            # Set the current frame id to use the camera matrices from the respective frame
            self.current_frame_id = i

            # Compute frustum coordinates
            self.update_matrices(width, height)
            V = self.get_view_matrix()
            P = self.get_projection_matrix()
            ndc_from_world = P @ V
            world_from_ndc = np.linalg.inv(ndc_from_world)

            def transform(x):
                v = world_from_ndc @ np.append(x, 1.0)
                return v[:3] / v[3]

            # Comput z coordinate of a point at the given distance
            world_p = self.position + self.forward *  distance
            ndc_p = (ndc_from_world @ np.concatenate([world_p, np.array([1])]))

            # Compute z after perspective division
            z = ndc_p[2] / ndc_p[3]

            lines = np.array([
                [-1, -1, -1], [-1,  1, -1],
                [-1, -1,  z], [-1,  1,  z],
                [ 1, -1, -1], [ 1,  1, -1],
                [ 1, -1,  z], [ 1,  1,  z],
                
                [-1, -1, -1], [-1, -1, z],
                [-1,  1, -1], [-1,  1, z],
                [ 1, -1, -1], [ 1, -1, z],
                [ 1,  1, -1], [ 1,  1, z],
                
                [-1, -1, -1], [ 1, -1, -1],
                [-1, -1,  z], [ 1, -1,  z],
                [-1,  1, -1], [ 1,  1, -1],
                [-1,  1,  z], [ 1,  1,  z],
            ], dtype=np.float32)

            lines = np.apply_along_axis(transform, 1, lines)
            all_lines[i] = lines

        self.frustum = Lines(all_lines, r_base=0.005, mode='lines', color=(0.1, 0.1, 0.1, 1), cast_shadow=False)
        self.add(self.frustum, show_in_hierarchy=False)

        orientation = np.array([ self.right,  self.up, self.forward ]).T
        self.origin = RigidBodies(self.position[np.newaxis], orientation[np.newaxis])
        self.add(self.origin, show_in_hierarchy=False)

        self.current_frame_id = frame_id

    def render_outline(self, ctx, camera, prog):
        # Only render the mesh outline, this avoids outlining 
        # the frustum and coordinate system visualization.
        self.mesh.render_outline(ctx, camera, prog)

    def view_from_camera(self):
        """If the viewer is specified for this camera, change the current view to view from this camera"""
        if self.viewer:
            self.hide_frustum()
            self.viewer.set_temp_camera(self)

    def gui(self, imgui):
        if self.viewer:
            if imgui.button("View from camera"):
                self.view_from_camera()
    
    def gui_context_menu(self, imgui):
        if self.viewer:
            if imgui.menu_item("View from camera", shortcut=None, selected=False, enabled=True)[1]:
                self.view_from_camera()


class WeakPerspectiveCamera(Camera):
    """ 
    A sequence of weak perspective cameras.
    The camera is positioned at (0,0,1) axis aligned and looks towards the negative z direction following the OpenGL conventions.
    """
    def __init__(self, scale, translation, cols, rows, near=C.znear, far=C.zfar, viewer=None, **kwargs):
        """ Initializer.
        :param scale: A np array of scale parameters [sx, sy] of shape (2) or a sequence of parameters of shape (N, 2)
        :param translation: A np array of translation parameters [tx, ty] of shape (2) or a sequence of parameters of shape (N, 2)
        :param cols: Number of columns in an image captured by this camera, used for computing the aspect ratio of the camera
        :param rows: Number of rows in an image captured by this camera, used for computing the aspect ratio of the camera
        :param near: Distance of the near plane from the camera
        :param far: Distance of the far plane from the camera
        :param viewer: the current viewer, if not None the gui for this object will show a button for viewing from this camera in the viewer
         """
        if len(scale.shape) == 1:
            scale = scale[np.newaxis]
            
        if len(translation.shape) == 1:
            translation = translation[np.newaxis]
        
        assert scale.shape[0] == translation.shape[0], "Number of frames in scale and translation must match"
        
        super(WeakPerspectiveCamera, self).__init__(n_frames=scale.shape[0], viewer=viewer, **kwargs)

        self.scale = scale
        self.translation = translation

        self.cols = cols
        self.rows = rows
        self.near = near
        self.far = far
        self.viewer = viewer

        self.position =  np.array([0, 0, 1], dtype=np.float32)
        self._right   =  np.array([1, 0, 0], dtype=np.float32)
        self._up      =  np.array([0, 1, 0], dtype=np.float32)
        self._forward = -np.array([0, 0, 1], dtype=np.float32)
    
    @property
    def forward(self):
        return self._forward
    
    @property
    def up(self):
        return self._up
    
    @property
    def right(self):
        return self._right

    def update_matrices(self, width, height):
        sx, sy = self.scale[self.current_frame_id]
        tx, ty = self.translation[self.current_frame_id]

        window_ar = width / height
        camera_ar = self.cols / self.rows
        ar = camera_ar / window_ar

        P = np.array([
            [sx * ar,  0, 0,  tx * sx * ar],
            [      0, sy, 0,      -ty * sy],
            [      0, 0, -1,             0],
            [      0, 0,  0,             1],
        ])

        znear, zfar = self.near, self.far
        P[2][2] = 2.0 / (znear - zfar)
        P[2][3] = (zfar + znear) / (znear - zfar)

        V = look_at(self.position, self.forward, np.array([0, 1, 0]))

        # Update camera matrices
        self.projection_matrix = P.astype('f4')
        self.view_matrix = V.astype('f4')
        self.view_projection_matrix = np.matmul(P, V).astype('f4')
    
    def get_projection_matrix(self):
        if self.projection_matrix is None:
            raise ValueError("update_matrices() must be called before to update the projection matrix")
        return self.projection_matrix
    
    def get_view_matrix(self):
        if self.view_matrix is None:
            raise ValueError("update_matrices() must be called before to update the view matrix")
        return self.view_matrix

    def get_view_projection_matrix(self):
        if self.view_projection_matrix is None:
            raise ValueError("update_matrices() must be called before to update the view-projection matrix")
        return self.view_projection_matrix
    
    @hooked
    def gui(self, imgui):
        u, show = imgui.checkbox("Show frustum", self.frustum is not None)
        if u:
            if show:
                self.show_frustum(self.cols, self.rows, self.far)
            else:
                self.hide_frustum()

    @hooked
    def gui_context_menu(self, imgui):
        u, show = imgui.menu_item("Show frustum", shortcut=None, selected=self.frustum is not None, enabled=True)
        if u:
            if show:
                self.show_frustum(self.cols, self.rows, self.far)
            else:
                self.hide_frustum()
        

class OpenCVCamera(Camera):
    """ A camera described by extrinsics and intrinsics in the format used by OpenCV """

    def __init__(self, K, Rt, cols, rows, dist_coeffs=None, near=C.znear, far=C.zfar, viewer=None, **kwargs):
        """ Initializer.
        :param K:  A np array of camera intrinsics in the format used by OpenCV (3, 3)
        :param Rt: A np array of camera extrinsics in the format used by OpenCV (3, 4)
        :param dist_coeffs: Lens distortion coefficients in the format used by OpenCV.
        :param cols: Width  of the image in pixels, matching the size of the image expected by the intrinsics matrix
        :param rows: Height of the image in pixels, matching the size of the image expected by the intrinsics matrix
        :param near: Distance of the near plane from the camera
        :param far: Distance of the far plane from the camera
        :param viewer: The current viewer, if not None the gui for this object will show a button for viewing from this camera in the viewer
         """
        rot = np.copy(Rt[:, 0:3].T)
        pos = -rot @ Rt[:, 3]

        rot[:, 1:] *= -1.0
        
        super(OpenCVCamera, self).__init__(position=pos, rotation=rot, viewer=viewer, **kwargs)

        self.K = K
        self.Rt = Rt
        self.dist_coeffs = dist_coeffs

        self.cols = cols
        self.rows = rows

        self.near = near
        self.far = far

    @property
    def position(self):
        return -self.Rt[:, 0:3].T @ self.Rt[:, 3]

    @property
    def forward(self):
        return self.Rt[2, :3]
    
    @property
    def up(self):
        return -self.Rt[1, :3]
    
    @property
    def right(self):
        return self.Rt[0, :3]
    
    def compute_opengl_view_projection(self, width, height):
        # Construct view and projection matrices which follow OpenGL conventions.
        # Adapted from https://amytabb.com/tips/tutorials/2019/06/28/OpenCV-to-OpenGL-tutorial-essentials/

        # Compute view matrix V
        lookat = np.copy(self.Rt)
        # Invert Y -> flip image bottom to top
        # Invert Z -> OpenCV has positive Z forward, we use negative Z forward
        lookat[1:3, :] *= -1.0
        V = np.vstack((lookat, np.array([0, 0, 0, 1])))

        # Compute projection matrix P
        K = self.K
        rows, cols = self.rows, self.cols
        near, far = self.near, self.far

        # Compute number of columns that we would need in the image to preserve the aspect ratio
        window_cols =  width / height * rows

        # Offset to center the image on the x direction
        x_offset = (window_cols - cols) * 0.5
        
        # Calibration matrix with added Z information and adapted to OpenGL coordinate
        # system which has (0,0) at center and Y pointing up
        Kgl = np.array([
            [-K[0,0],       0, -(cols - K[0, 2]) - x_offset,            0],
            [      0, -K[1,1],             (rows - K[1, 2]),            0],
            [      0,       0,                -(near + far),-(near * far)],
            [      0,       0,                           -1,            0],
        ])

        # Transformation from pixel coordinates to normalized device coordinates used by OpenGL
        NDC = np.array([
            [-2 / window_cols,         0,                 0,                             1],
            [               0, -2 / rows,                 0,                            -1],
            [               0,         0,  2 / (far - near),  -(far + near) / (far - near)],
            [               0,         0,                 0,                             1],
        ])

        P = NDC @ Kgl

        return V, P

    def update_matrices(self, width, height):
        V, P = self.compute_opengl_view_projection(width, height)

        #Update camera matrices
        self.projection_matrix = P.astype('f4')
        self.view_matrix = V.astype('f4')
        self.view_projection_matrix = np.matmul(P, V).astype('f4')

    def get_projection_matrix(self):
        if self.projection_matrix is None:
            raise ValueError("update_matrices() must be called before to update the projection matrix")
        return self.projection_matrix
    
    def get_view_matrix(self):
        if self.view_matrix is None:
            raise ValueError("update_matrices() must be called before to update the view matrix")
        return self.view_matrix

    def get_view_projection_matrix(self):
        if self.view_projection_matrix is None:
            raise ValueError("update_matrices() must be called before to update the view-projection matrix")
        return self.view_projection_matrix

    @hooked
    def gui(self, imgui):
        u, show = imgui.checkbox("Show frustum", self.frustum is not None)
        if u:
            if show:
                self.show_frustum(self.cols, self.rows, self.far)
            else:
                self.hide_frustum()
    
    @hooked
    def gui_context_menu(self, imgui):
        u, show = imgui.menu_item("Show frustum", shortcut=None, selected=self.frustum is not None, enabled=True)
        if u:
            if show:
                self.show_frustum(self.cols, self.rows, self.far)
            else:
                self.hide_frustum()        


class PinholeCamera(CameraInterface):
    """
    Your classic pinhole camera.
    """

    def __init__(self, fov=45, orthographic=None, znear=C.znear, zfar=C.zfar):
        self.fov = fov
        self.is_ortho = orthographic is not None
        self.ortho_size = 1.0 if orthographic is None else orthographic

        # Default camera settings.
        self._position = np.array([0.0, 0.0, 2.5])
        self.target = np.array([0.0, 0.0, 0.0])
        self._up = np.array([0.0, 1.0, 0.0])

        self.ZOOM_FACTOR = 4
        self.ROT_FACTOR = 0.0025
        self.PAN_FACTOR = 0.01

        self.near = znear
        self.far = zfar

        # GUI options
        self.name = 'Camera'
        self.icon = '\u0084'

    @property
    def position(self):
        return self._position

    @position.setter
    def position(self, position):
        self._position = position

    @property
    def forward(self):
        forward = self.target - self.position
        return forward / np.linalg.norm(forward)

    @property
    def up(self):
        return self._up

    @up.setter
    def up(self, up):
        self._up = up

    @property
    def right(self):
        return np.cross(self.up, self.forward)

    def save_cam(self):
        """Saves the current camera parameters"""
        cam_dir = C.export_dir + '/camera_params/'
        if not os.path.exists(cam_dir):
            os.makedirs(cam_dir)

        cam_dict = {}
        cam_dict['position'] = self.position
        cam_dict['target'] = self.target
        cam_dict['up'] = self.up
        cam_dict['ZOOM_FACTOR'] = self.ZOOM_FACTOR
        cam_dict['ROT_FACTOR'] = self.ROT_FACTOR
        cam_dict['PAN_FACTOR'] = self.PAN_FACTOR
        cam_dict['near'] = self.near
        cam_dict['far'] = self.far

        joblib.dump(cam_dict, cam_dir+'cam_params.pkl')

    def load_cam(self):
        """Loads the camera parameters"""
        cam_dir = C.export_dir + '/camera_params/'
        if not os.path.exists(cam_dir):
            print('camera config does not exist')
        else:
            cam_dict = joblib.load(cam_dir + 'cam_params.pkl')
            self.position = cam_dict['position']
            self.target = cam_dict['target']
            self.up = cam_dict['up']
            self.ZOOM_FACTOR = cam_dict['ZOOM_FACTOR']
            self.ROT_FACTOR = cam_dict['ROT_FACTOR']
            self.PAN_FACTOR = cam_dict['PAN_FACTOR']
            self.near = cam_dict['near']
            self.far = cam_dict['far']

    def update_matrices(self, width, height):
        #Compute projection matrix
        if self.is_ortho:
            yscale = self.ortho_size
            xscale = width / height * yscale
            P = orthographic_projection(xscale, yscale, self.near, self.far)
        else:
            P = perspective_projection(np.deg2rad(self.fov), width / height, self.near, self.far)
        
        #Compute view matrix
        V = look_at(self.position, self.target, self.up)

        #Update camera matrices
        self.projection_matrix = P.astype('f4')
        self.view_matrix = V.astype('f4')
        self.view_projection_matrix = np.matmul(P, V).astype('f4')

    def get_projection_matrix(self):
        if self.projection_matrix is None:
            raise ValueError("update_matrices() must be called before to update the projection matrix")
        return self.projection_matrix
    
    def get_view_matrix(self):
        if self.view_matrix is None:
            raise ValueError("update_matrices() must be called before to update the view matrix")
        return self.view_matrix

    def get_view_projection_matrix(self):
        if self.view_projection_matrix is None:
            raise ValueError("update_matrices() must be called before to update the view-projection matrix")
        return self.view_projection_matrix

    def dolly_zoom(self, speed, move_target=False):
        """
        Zoom by moving the camera along its view direction.
        If move_target is true the camera target will also move rigidly with the camera.
        """
        # We update both the orthographic and perspective projection so that the transition is seamless when
        # transitioning between them.
        self.ortho_size -= 0.1 * np.sign(speed)
        self.ortho_size = max(0.0001, self.ortho_size)

        # Scale the speed in proportion to the norm (i.e. camera moves slower closer to the target)
        norm = max(np.linalg.norm(self.position - self.target), 2)
        fwd = self.forward

        # Adjust speed according to config
        speed *= C.camera_zoom_speed

        if move_target:
            self.position += fwd * speed * norm
            self.target += fwd * speed * norm
        else:
            # Clamp movement size to avoid surpassing the target
            movement_length = speed * norm 
            max_movement_length = max(np.linalg.norm(self.target - self.position) - 0.01, 0.0)

            # Update position
            self.position += fwd * min(movement_length, max_movement_length)
        
    def pan(self, mouse_dx, mouse_dy):
        """Move the camera in the image plane."""
        sideways = np.cross(self.forward, self.up)
        up = np.cross(sideways, self.forward)

        # scale speed according to distance from target
        speed = max(np.linalg.norm(self.target - self.position) * 0.1, 0.1)
        
        speed_x = mouse_dx * self.PAN_FACTOR * speed
        speed_y = mouse_dy * self.PAN_FACTOR * speed

        self.position -= sideways * speed_x
        self.target -= sideways * speed_x

        self.position += up * speed_y
        self.target += up * speed_y

    def rotate_azimuth_elevation(self, mouse_dx, mouse_dy):
        """Rotate the camera left-right and up-down (roll is not allowed)."""
        cam_pose = np.linalg.inv(self.view_matrix)

        z_axis = cam_pose[:3, 2]
        dot = np.dot(z_axis, self.up)
        rot = np.eye(4)

        # Avoid singularity when z axis of camera is aligned with the up axis of the scene.
        if not (mouse_dy > 0 and dot > 0 and 1 - dot < 0.001) and not (mouse_dy < 0 and dot < 0 and 1 + dot < 0.001):
            # We are either hovering exactly below or above the scene's target but we want to move away or we are
            # not hitting the singularity anyway.
            x_axis = cam_pose[:3, 0]
            rot_x = rotation_matrix(self.ROT_FACTOR * -mouse_dy, x_axis, self.target)
            rot = rot_x @ rot

        y_axis = cam_pose[:3, 1]
        x_speed = self.ROT_FACTOR / 10 if 1 - np.abs(dot) < 0.01 else self.ROT_FACTOR
        rot = rotation_matrix(x_speed * -mouse_dx, y_axis, self.target) @ rot

        self.position = _transform_vector(rot, self.position)

    def rotate_azimuth(self, angle):
        """Rotate around camera's up-axis by given angle (in radians)."""
        if np.abs(angle) < 1e-8:
            return
        cam_pose = np.linalg.inv(self.view_matrix)
        y_axis = cam_pose[:3, 1]
        rot = rotation_matrix(angle, y_axis, self.target)
        self.position = _transform_vector(rot, self.position)

    def get_ray(self, x, y, width, height):
        """Construct a ray going through the middle of the given pixel."""
        w, h = width, height

        # Pixel in (-1, 1) range.
        screen_x = (2 * (x + 0.5) / w - 1)
        screen_y = (1 - 2 * (y + 0.5) / h)

        # Scale to actual image plane size.
        scale = self.ortho_size if self.is_ortho else np.tan(np.deg2rad(self.fov) / 2)
        screen_x *= scale * w / h
        screen_y *= scale

        pixel_2d = np.array([screen_x, screen_y, 0 if self.is_ortho else -1])
        cam2world = np.linalg.inv(self.view_matrix)
        pixel_3d = _transform_vector(cam2world, pixel_2d)
        if self.is_ortho:
            ray_origin = pixel_3d
            ray_dir = self.forward
        else:
            eye_origin = np.zeros(3)
            ray_origin = _transform_vector(cam2world, eye_origin)
            ray_dir = pixel_3d - ray_origin
        ray_dir = ray_dir / np.linalg.norm(ray_dir)

        return ray_origin, ray_dir

    def gui(self, imgui):
        _, self.is_ortho = imgui.checkbox('Orthographic Camera', self.is_ortho)
        _, self.fov = imgui.slider_float('Camera FOV##fov', self.fov, 0.1, 180.0, '%.1f')
