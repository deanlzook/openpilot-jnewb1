import math
import numpy as np
import time

from openpilot.tools.sim.bridge.common import World, SimulatorBridge
from openpilot.tools.sim.lib.common import vec3, SimulatorState
from openpilot.tools.sim.lib.camerad import W, H


SIZE = 2 ** 11

def crop_center(img, cropx, cropy):
    y, x, *_ = img.shape
    startx = x // 2 - (cropx // 2)
    starty = y // 2 - (cropy // 2)    
    return img[starty:starty + cropy, startx:startx + cropx, ...]


class MetaDriveWorld(World):
  def __init__(self, env, ticks_per_frame: float, dual_camera = False):
    super().__init__(dual_camera)
    self.env = env
    self.ticks_per_frame = ticks_per_frame
    self.dual_camera = dual_camera

    self.steer_ratio = 15

    self.vc = [0.0,0.0]

    self.reset_time = 0

  def get_cam_as_rgb(self, cam):
    cam = self.env.engine.sensors[cam]
    img = cam.perceive(self.env.vehicle, clip=False)
    if type(img) != np.ndarray:
      img = img.get() # convert cupy array to numpy
    return img

  def get_obs_as_rgb(self, buffer):
    return (self.env.observations['default_agent'].img_obs.state * 255)[...,-1]

  def apply_controls(self, steer_angle, throttle_out, brake_out):
    steer_metadrive = steer_angle * 1 / (self.env.vehicle.MAX_STEERING * self.steer_ratio)
    steer_metadrive = np.clip(steer_metadrive, -1, 1)

    if (time.monotonic() - self.reset_time) > 5:
      self.vc[0] = steer_metadrive

      if throttle_out:
        self.vc[1] = throttle_out/10
      else:
        self.vc[1] = -brake_out
    else:
      self.vc[0] = 0
      self.vc[1] = 0

  def read_sensors(self, state: SimulatorState):
    state.velocity = vec3(x=float(self.env.vehicle.velocity[0]), y=float(self.env.vehicle.velocity[1]), z=0)
    state.gps.from_xy(self.env.vehicle.position)
    state.bearing = float(math.degrees(self.env.vehicle.heading_theta))
    state.steering_angle = self.env.vehicle.steering * self.env.vehicle.MAX_STEERING
    state.valid = True

  def read_cameras(self):
    if self.dual_camera:
     self.wide_road_image = crop_center(self.get_cam_as_rgb("rgb_wide"), W, H)
    self.road_image = crop_center(self.get_cam_as_rgb("rgb_road"), W, H)

  def tick(self):
    obs, _, terminated, _, info = self.env.step(self.vc)

    if terminated:
      self.env.reset()
      self.reset_time = time.monotonic()

  def close(self):
    pass


class MetaDriveBridge(SimulatorBridge):
  TICKS_PER_FRAME = 2

  def __init__(self, args):
    self.should_render = True

    super(MetaDriveBridge, self).__init__(args)

  def spawn_world(self):
    from metadrive.component.sensors.rgb_camera import RGBCamera
    from metadrive.component.sensors.base_camera import _cuda_enable
    from metadrive.engine.core.engine_core import EngineCore
    from metadrive.engine.core.image_buffer import ImageBuffer
    from metadrive.envs.metadrive_env import MetaDriveEnv
    from panda3d.core import Vec3

    # By default, metadrive won't try to use cuda images unless it's used as a sensor for vehicles, so patch that in
    def add_image_sensor_patched(self, name: str, cls, args):
      if self.global_config["image_on_cuda"]:# and name == self.global_config["vehicle_config"]["image_source"]:
          sensor = cls(*args, self, cuda=True)
      else:
          sensor = cls(*args, self, cuda=False)
      assert isinstance(sensor, ImageBuffer), "This API is for adding image sensor"
      self.sensors[name] = sensor

    def get_rgb_array_cpu(self):
      origin_img = self.buffer.getDisplayRegion(0).getScreenshot()
      img = np.frombuffer(origin_img.getRamImage().getData(), dtype=np.uint8)
      img = img.reshape((origin_img.getYSize(), origin_img.getXSize(), 4))
      # img = np.swapaxes(img, 1, 0)
      img = img[..., :-1]
      return img

    EngineCore.add_image_sensor = add_image_sensor_patched
    ImageBuffer.get_rgb_array_cpu = get_rgb_array_cpu

    C3_POSITION = Vec3(0, 0, 1)

    class RGBCameraWide(RGBCamera):
      def __init__(self, *args, **kwargs):
        super(RGBCameraWide, self).__init__(*args, **kwargs)
        cam = self.get_cam()
        cam.setPos(C3_POSITION)
        lens = self.get_lens()
        lens.setFov(160)

    class RGBCameraRoad(RGBCamera):
      def __init__(self, *args, **kwargs):
        super(RGBCameraRoad, self).__init__(*args, **kwargs)
        cam = self.get_cam()
        cam.setPos(C3_POSITION)
        lens = self.get_lens()
        lens.setFov(40)

    sensors = {
      "rgb_road": (RGBCameraRoad, SIZE, SIZE, )
    }

    if self.dual_camera:
      sensors["rgb_wide"] = (RGBCameraWide, SIZE, SIZE)

    env = MetaDriveEnv(
        dict(
          use_render=self.should_render,
          vehicle_config=dict(
            enable_reverse=False,
            image_source="rgb_road",
            spawn_longitude=15
          ),
          sensors=sensors,
          image_on_cuda=_cuda_enable,
          image_observation=True,
          interface_panel=[],
          out_of_route_done=False,
          on_continuous_line_done=False,
          crash_vehicle_done=False,
          crash_object_done=False,
        )
      )

    env.reset()

    return MetaDriveWorld(env, self.TICKS_PER_FRAME)