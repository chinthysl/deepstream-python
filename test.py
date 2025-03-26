import sys
sys.path.append('../')
import os
import gi
gi.require_version('Gst', '1.0')
from gi.repository import GLib, Gst
from common.bus_call import bus_call
import pyds
import configparser
from typing import Tuple, List, Dict
import time

# Update class IDs according to PeopleNet labels
PGIE_CLASS_ID_PERSON = 0
PGIE_CLASS_ID_BAG = 1
PGIE_CLASS_ID_FACE = 2

class BBox:
    def __init__(self, left: float, top: float, width: float, height: float):
        self.left = left
        self.top = top
        self.width = width
        self.height = height

    def get_area(self) -> float:
        return self.width * self.height

    def get_intersection(self, other: 'BBox') -> float:
        x1 = max(self.left, other.left)
        y1 = max(self.top, other.top)
        x2 = min(self.left + self.width, other.left + other.width)
        y2 = min(self.top + self.height, other.top + other.height)

        if x1 >= x2 or y1 >= y2:
            return 0.0

        intersection_area = (x2 - x1) * (y2 - y1)
        return intersection_area

class ROIInspector:
    def __init__(self, roi_coords: List[float], timeout: float):
        self.roi = BBox(roi_coords[0], roi_coords[1], roi_coords[2], roi_coords[3])
        self.timeout = timeout
        self.track_timestamps: Dict[int, float] = {}  # track_id -> first_detection_time
        self.alerted_tracks: set = set()
        self.active_alerts: set = set()  # Currently active alerts (still in ROI)

    def check_intersection(self, bbox: BBox) -> float:
        intersection_area = bbox.get_intersection(self.roi)
        return intersection_area / bbox.get_area()

    def update(self, track_id: int, bbox: BBox) -> bool:
        intersection_ratio = self.check_intersection(bbox)
        current_time = time.time()

        print(f"Track {track_id}: intersection ratio = {intersection_ratio}")

        if intersection_ratio > 0.5:
            if track_id not in self.track_timestamps:
                print(f"Track {track_id}: entered ROI")
                self.track_timestamps[track_id] = current_time
            
            # If already alerted, keep the alert active
            if track_id in self.alerted_tracks:
                self.active_alerts.add(track_id)
                return True
                
            elif track_id not in self.alerted_tracks:
                time_in_roi = current_time - self.track_timestamps[track_id]
                print(f"Track {track_id}: time in ROI = {time_in_roi:.2f}s")
                if time_in_roi >= self.timeout:
                    print(f"Track {track_id}: ALERT!")
                    self.alerted_tracks.add(track_id)
                    self.active_alerts.add(track_id)
                    return True
        else:
            if track_id in self.track_timestamps:
                print(f"Track {track_id}: left ROI")
                del self.track_timestamps[track_id]
            self.active_alerts.discard(track_id)  # Remove from active alerts when outside ROI

        return False

    def has_active_alerts(self) -> bool:
        return len(self.active_alerts) > 0

class Pipeline:
    def __init__(self, roi_coords: List[float], timeout: float):
        self.pipeline = None
        self.loop = None
        self.past_tracking_meta = [0]
        self.roi_inspector = ROIInspector(roi_coords, timeout)
        
    def create_source_bin(self, index, uri):
        print("Creating source bin")
        bin_name = "source-bin-%02d" % index
        nbin = Gst.Bin.new(bin_name)
        if not nbin:
            sys.stderr.write(" Unable to create source bin \n")

        uri_decode_bin = Gst.ElementFactory.make("uridecodebin", "uri-decode-bin")
        if not uri_decode_bin:
            sys.stderr.write(" Unable to create uri decode bin \n")

        uri_decode_bin.set_property("uri", uri)
        uri_decode_bin.connect("pad-added", self.cb_newpad, nbin)
        uri_decode_bin.connect("child-added", self.decodebin_child_added, nbin)

        Gst.Bin.add(nbin, uri_decode_bin)
        bin_pad = nbin.add_pad(Gst.GhostPad.new_no_target("src", Gst.PadDirection.SRC))
        if not bin_pad:
            sys.stderr.write(" Failed to add ghost pad in source bin \n")
            return None
        return nbin

    def cb_newpad(self, decodebin, decoder_src_pad, data):
        print("In cb_newpad\n")
        caps = decoder_src_pad.get_current_caps()
        if not caps:
            caps = decoder_src_pad.query_caps()
        gststruct = caps.get_structure(0)
        gstname = gststruct.get_name()
        source_bin = data
        features = caps.get_features(0)

        print("gstname=", gstname)
        if(gstname.find("video")!=-1):
            print("features=", features)
            if features.contains("memory:NVMM"):
                bin_ghost_pad = source_bin.get_static_pad("src")
                if not bin_ghost_pad.set_target(decoder_src_pad):
                    sys.stderr.write("Failed to link decoder src pad to source bin ghost pad\n")
            else:
                sys.stderr.write(" Error: Decodebin did not pick nvidia decoder plugin.\n")

    def decodebin_child_added(self, child_proxy, Object, name, user_data):
        print("Decodebin child added:", name, "\n")
        if(name.find("decodebin") != -1):
            Object.connect("child-added", self.decodebin_child_added, user_data)
        
        if "source" in name:
            source_element = child_proxy.get_by_name("source")
            if source_element.find_property('drop-on-latency') != None:
                Object.set_property("drop-on-latency", True)

    def osd_sink_pad_buffer_probe(self, pad, info, u_data):
        frame_number=0
        num_rects=0

        gst_buffer = info.get_buffer()
        if not gst_buffer:
            print("Unable to get GstBuffer ")
            return

        # Retrieve batch metadata from the gst_buffer
        # Note that pyds.gst_buffer_get_nvds_batch_meta() expects the
        # C address of gst_buffer as input, which is obtained with hash(gst_buffer)
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
        l_frame = batch_meta.frame_meta_list
        while l_frame is not None:
            try:
                # Note that l_frame.data needs a cast to pyds.NvDsFrameMeta
                # The casting is done by pyds.NvDsFrameMeta.cast()
                # The casting also keeps ownership of the underlying memory
                # in the C code, so the Python garbage collector will leave
                # it alone.
                frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
            except StopIteration:
                break

            # Initialize object counter with PeopleNet classes
            obj_counter = {
                PGIE_CLASS_ID_PERSON: 0,
                PGIE_CLASS_ID_BAG: 0,
                PGIE_CLASS_ID_FACE: 0
            }
            frame_number=frame_meta.frame_num
            num_rects = frame_meta.num_obj_meta
            l_obj=frame_meta.obj_meta_list
            while l_obj is not None:
                try:
                    # Casting l_obj.data to pyds.NvDsObjectMeta
                    obj_meta=pyds.NvDsObjectMeta.cast(l_obj.data)
                except StopIteration:
                    break
                obj_counter[obj_meta.class_id] += 1

                # Only process PERSON class
                if obj_meta.class_id == PGIE_CLASS_ID_PERSON:
                    bbox = BBox(
                        obj_meta.rect_params.left,
                        obj_meta.rect_params.top,
                        obj_meta.rect_params.width,
                        obj_meta.rect_params.height
                    )

                    # Check if person is in ROI for too long
                    is_alert = self.roi_inspector.update(obj_meta.object_id, bbox)
                    
                    if is_alert:
                        # Set red color for alert
                        obj_meta.rect_params.border_color.set(1.0, 0.0, 0.0, 0.8)
                        obj_meta.text_params.display_text = f"ALERT! ID={obj_meta.object_id} {obj_meta.obj_label}"
                    else:
                        # Set default blue color
                        obj_meta.rect_params.border_color.set(0.0, 0.0, 1.0, 0.8)
                        obj_meta.text_params.display_text = f"ID={obj_meta.object_id} {obj_meta.obj_label}"

                try: 
                    l_obj=l_obj.next
                except StopIteration:
                    break

            # Acquiring a display meta object
            display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
            display_meta.num_labels = 3  # Changed to 3 for ROI label and alert message
            py_nvosd_text_params = display_meta.text_params[0]

            # Setting display text to be shown on screen
            py_nvosd_text_params.display_text = "Frame Number={} Number of Objects={} Person_count={} Bag_count={} Face_count={}".format(
                frame_number, num_rects, 
                obj_counter[PGIE_CLASS_ID_PERSON],
                obj_counter[PGIE_CLASS_ID_BAG],
                obj_counter[PGIE_CLASS_ID_FACE]
            )
            # Now set the offsets where the string should appear
            py_nvosd_text_params.x_offset = 10
            py_nvosd_text_params.y_offset = 12

            # Font , font-color and font-size
            py_nvosd_text_params.font_params.font_name = "Serif"
            py_nvosd_text_params.font_params.font_size = 10
            # set(red, green, blue, alpha); set to White
            py_nvosd_text_params.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)

            # Text background color
            py_nvosd_text_params.set_bg_clr = 1
            # set(red, green, blue, alpha); set to Black
            py_nvosd_text_params.text_bg_clr.set(0.0, 0.0, 0.0, 1.0)

            # Add ROI rectangle display
            display_meta.num_rects = 1
            roi_rect_params = display_meta.rect_params[0]
            roi_rect_params.left = self.roi_inspector.roi.left
            roi_rect_params.top = self.roi_inspector.roi.top
            roi_rect_params.width = self.roi_inspector.roi.width
            roi_rect_params.height = self.roi_inspector.roi.height
            roi_rect_params.border_width = 2
            
            # Change ROI color to red if there are active alerts
            if self.roi_inspector.has_active_alerts():
                roi_rect_params.border_color.set(1.0, 0.0, 0.0, 0.8)  # Red color for active alert
            else:
                roi_rect_params.border_color.set(0.0, 1.0, 0.0, 0.8)  # Green color for normal state
            
            roi_rect_params.has_bg_color = 0
            roi_rect_params.bg_color.set(0.0, 0.0, 0.0, 0.0)

            # Add ROI label
            roi_text_params = display_meta.text_params[1]
            roi_text_params.display_text = "ROI"
            roi_text_params.x_offset = int(self.roi_inspector.roi.left)
            roi_text_params.y_offset = int(self.roi_inspector.roi.top - 10)
            roi_text_params.font_params.font_name = "Serif"
            roi_text_params.font_params.font_size = 15
            roi_text_params.font_params.font_color.set(0.0, 1.0, 0.0, 1.0)
            roi_text_params.set_bg_clr = 1
            roi_text_params.text_bg_clr.set(0.0, 0.0, 0.0, 1.0)

            # Add Alert Message if there are active alerts
            alert_text_params = display_meta.text_params[2]
            if self.roi_inspector.has_active_alerts():
                alert_text_params.display_text = "⚠ ALERT: Person(s) in restricted area!"
                alert_text_params.x_offset = 10
                alert_text_params.y_offset = 50  # Below the frame info
                alert_text_params.font_params.font_name = "Serif"
                alert_text_params.font_params.font_size = 20
                alert_text_params.font_params.font_color.set(1.0, 0.0, 0.0, 1.0)  # Red color
                alert_text_params.set_bg_clr = 1
                alert_text_params.text_bg_clr.set(0.0, 0.0, 0.0, 0.8)  # Semi-transparent black background
            else:
                alert_text_params.display_text = ""  # No alert message when no active alerts

            # Using pyds.get_string() to get display_text as string
            print(pyds.get_string(py_nvosd_text_params.display_text))
            pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)

            try:
                l_frame=l_frame.next
            except StopIteration:
                break
            
        # Add past tracking meta data processing
        if(self.past_tracking_meta[0]==1):
            l_user=batch_meta.batch_user_meta_list
            while l_user is not None:
                try:
                    user_meta=pyds.NvDsUserMeta.cast(l_user.data)
                except StopIteration:
                    break
                if(user_meta and user_meta.base_meta.meta_type==pyds.NvDsMetaType.NVDS_TRACKER_PAST_FRAME_META):
                    try:
                        pPastFrameObjBatch = pyds.NvDsPastFrameObjBatch.cast(user_meta.user_meta_data)
                    except StopIteration:
                        break
                    for trackobj in pyds.NvDsPastFrameObjBatch.list(pPastFrameObjBatch):
                        print("streamId=",trackobj.streamID)
                        print("surfaceStreamID=",trackobj.surfaceStreamID)
                        for pastframeobj in pyds.NvDsPastFrameObjStream.list(trackobj):
                            print("numobj=",pastframeobj.numObj)
                            print("uniqueId=",pastframeobj.uniqueId)
                            print("classId=",pastframeobj.classId)
                            print("objLabel=",pastframeobj.objLabel)
                            for objlist in pyds.NvDsPastFrameObjList.list(pastframeobj):
                                print('frameNum:', objlist.frameNum)
                                print('tBbox.left:', objlist.tBbox.left)
                                print('tBbox.width:', objlist.tBbox.width)
                                print('tBbox.top:', objlist.tBbox.top)
                                print('tBbox.right:', objlist.tBbox.height)
                                print('confidence:', objlist.confidence)
                                print('age:', objlist.age)
                try:
                    l_user=l_user.next
                except StopIteration:
                    break
            
        return Gst.PadProbeReturn.OK    

    def create_pipeline(self, video_path):
        # Standard GStreamer initialization
        Gst.init(None)

        # Create Pipeline
        print("Creating Pipeline \n ")
        self.pipeline = Gst.Pipeline()
        if not self.pipeline:
            sys.stderr.write(" Unable to create Pipeline \n")

        # Create elements
        streammux = Gst.ElementFactory.make("nvstreammux", "Stream-muxer")
        if not streammux:
            sys.stderr.write(" Unable to create NvStreamMux \n")

        self.pipeline.add(streammux)

        # Create and add source bin
        uri_name = "file://" + os.path.abspath(video_path)
        source_bin = self.create_source_bin(0, uri_name)
        if not source_bin:
            sys.stderr.write("Unable to create source bin \n")
        self.pipeline.add(source_bin)

        # Create and link elements
        elements = self.create_elements()
        if not all(elements.values()):
            sys.stderr.write("Failed to create elements\n")
            return False

        # Configure elements
        self.configure_elements(streammux, elements['pgie'], elements['tracker'])

        # Link elements
        self.link_elements(streammux, source_bin, elements)

        # Set up probe
        osdsinkpad = elements['nvosd'].get_static_pad("sink")
        if not osdsinkpad:
            sys.stderr.write(" Unable to get sink pad of nvosd \n")
        osdsinkpad.add_probe(Gst.PadProbeType.BUFFER, self.osd_sink_pad_buffer_probe, 0)

        return True

    def create_elements(self):
        elements = {
            'pgie': Gst.ElementFactory.make("nvinfer", "primary-inference"),
            'tracker': Gst.ElementFactory.make("nvtracker", "tracker"),
            'nvvidconv': Gst.ElementFactory.make("nvvideoconvert", "convertor"),
            'nvosd': Gst.ElementFactory.make("nvdsosd", "onscreendisplay"),
            'sink': Gst.ElementFactory.make("nv3dsink", "nv3d-sink")
        }
        
        for name, element in elements.items():
            if not element:
                sys.stderr.write(f" Unable to create {name}\n")
            else:
                self.pipeline.add(element)
                
        return elements

    def configure_elements(self, streammux, pgie, tracker):
        # Configure streammux
        if os.environ.get('USE_NEW_NVSTREAMMUX') != 'yes':
            streammux.set_property('width', 1920)
            streammux.set_property('height', 1080)
            streammux.set_property('batched-push-timeout', 4000000)
        streammux.set_property('batch-size', 1)

        # Configure pgie
        pgie.set_property('config-file-path', "config/config_infer_peoplenet.txt")

        # Configure tracker
        self.configure_tracker(tracker)

    def configure_tracker(self, tracker):
        config = configparser.ConfigParser()
        config.read('config/config_tracker.txt')

        tracker_properties = {
            'tracker-width': ('tracker-width', 'tracker-width'),
            'tracker-height': ('tracker-height', 'tracker-height'),
            'gpu-id': ('gpu_id', 'gpu-id'),
            'll-lib-file': ('ll-lib-file', 'll-lib-file'),
            'll-config-file': ('ll-config-file', 'll-config-file'),
            'enable-batch-process': ('enable_batch_process', 'enable-batch-process'),
            'enable-past-frame': ('enable_past_frame', 'enable-past-frame')
        }

        for key, (prop_name, config_name) in tracker_properties.items():
            if key in config['tracker']:
                value = config.getint('tracker', key) if key not in ['ll-lib-file', 'll-config-file'] else config.get('tracker', key)
                tracker.set_property(prop_name, value)
                if key == 'enable-past-frame':
                    self.past_tracking_meta[0] = value

    def link_elements(self, streammux, source_bin, elements):
        # Link source_bin to streammux
        padname = "sink_0"
        sinkpad = streammux.get_request_pad(padname)
        if not sinkpad:
            sys.stderr.write("Unable to create sink pad bin \n")
        srcpad = source_bin.get_static_pad("src")
        if not srcpad:
            sys.stderr.write("Unable to create src pad bin \n")
        srcpad.link(sinkpad)

        # Link the elements
        streammux.link(elements['pgie'])
        elements['pgie'].link(elements['tracker'])
        elements['tracker'].link(elements['nvvidconv'])
        elements['nvvidconv'].link(elements['nvosd'])
        elements['nvosd'].link(elements['sink'])

    def run(self):
        # Create an event loop and feed GStreamer bus messages to it
        self.loop = GLib.MainLoop()
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", bus_call, self.loop)

        # Start playing
        print("Starting pipeline \n")
        self.pipeline.set_state(Gst.State.PLAYING)
        try:
            self.loop.run()
        except:
            pass

        # Cleanup
        self.pipeline.set_state(Gst.State.NULL)

def main(args):
    # Check input arguments
    if len(args) != 7:
        sys.stderr.write("usage: %s <video file path> <roi_x> <roi_y> <roi_width> <roi_height> <timeout_seconds>\n" % args[0])
        sys.exit(1)

    try:
        video_path = args[1]
        roi_coords = [float(args[2]), float(args[3]), float(args[4]), float(args[5])]
        timeout = float(args[6])
    except ValueError:
        sys.stderr.write("Error: ROI coordinates and timeout must be numbers\n")
        sys.exit(1)

    pipeline = Pipeline(roi_coords, timeout)
    if pipeline.create_pipeline(video_path):
        pipeline.run()

if __name__ == '__main__':
    sys.exit(main(sys.argv))
