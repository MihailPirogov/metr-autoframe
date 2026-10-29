"""Download the YuNet face-detection model (OpenCV Zoo, MIT license) next to app.py."""
import urllib.request
URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
urllib.request.urlretrieve(URL, "face_detection_yunet_2023mar.onnx")
print("ok: face_detection_yunet_2023mar.onnx")
