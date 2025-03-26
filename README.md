# DeepStream People Detection and ROI Monitoring

This project implements a DeepStream pipeline for monitoring people in multiple camera streams and detecting when they remain in specified regions of interest (ROIs) for extended periods (loitering alerts).

## Features

- Support for multiple video files
- PeopleNet-based person detection and tracking
- Configurable Regions of Interest (ROIs) with a timeout for loitering alerts
- Visual alerts when people remain in ROIs beyond timeout
- Multi-camera grid display
- Real-time visualization with bounding boxes and tracking IDs

## Pipeline

![DeepStream Pipeline](ds-pipeline.jpg)


## Prerequisites

- Deepstream setup with dGPU or Jeton device
- DeepStream SDK 6.2 or later
- Coresponding DeepStream Python bindings

## Usage

1. Clone this repository:
   ```bash
   git clone https://github.com/chinthysl/deepstream-python.git
   cd deepstream-python
   ```

2. Modify the camera streams and ROIs in `main.py`:
   ```python
   # Add cameras
   pipeline.add_camera(0, "rtsp://video1.mp4")
   pipeline.add_camera(1, "rtsp://video2.mp4")
   
   # Add ROIs with timeouts (in seconds)
   pipeline.add_roi(0, [(100, 100), (200, 100), (200, 200), (100, 200)], 5.0, 0)
   pipeline.add_roi(1, [(300, 300), (400, 300), (400, 400), (300, 400)], 3.0, 1)
   ```

2. Run the pipeline:
   ```bash
   python3 test.py <videofile> <roi x> <roi y> <roi width> <roi height> <timeout in sec>
   python3 test.py sample_1080p_h264.mp4 10 400 500 400 2
   ```

## Future development

- Enable multiple streams
- Dynamic addition of streams
- RTSP server activation or video recoding when loitering detected
- Deployment optimizations of edge devices to cater more video streams
- API and alert based control of the pipeline


## License

This project is licensed under the MIT License - see the LICENSE file for details. 