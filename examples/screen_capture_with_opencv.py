import dxcam
import cv2
import time

# Create a DXCamera instance
camera = dxcam.create()

# Set the region to capture
left, top = (1920 - 640) // 2, (1080 - 640) // 2
right, bottom = left + 640, top + 640
region = (left, top, right, bottom)

# Start the screen capture
camera.start(region=region)

# Initialize variables for FPS calculation
frames = 0
start_time = time.time()

while True:
    # Get the latest frame from the frame buffer
    frame = camera.get_latest_frame()
    
    # Display the FPS on the captured screen
    frames += 1
    if time.time() - start_time > 1:
        fps = frames / (time.time() - start_time)
        cv2.putText(frame, f"FPS: {fps:.2f}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
        frames = 0
        start_time = time.time()
    
    # Display the captured screen
    cv2.imshow("Screen Capture", frame)
    
    # Check if the user pressed 'q' to quit
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

# Stop the screen capture
camera.stop()

# Release the resources
cv2.destroyAllWindows()
