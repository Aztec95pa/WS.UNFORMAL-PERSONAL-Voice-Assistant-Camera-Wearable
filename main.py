import asyncio
import threading
import tkinter as tk
from tkinter import Tk, Button, Toplevel, Listbox, END, Label
from bleak import BleakScanner, BleakClient
from PIL import Image, ImageTk
import io
import json
import datetime
from image_receiver import ImageReceiver, InvalidImage, decode_jpeg

# -----------Speech to Text
from vosk import Model, KaldiRecognizer
model = Model(model_name="vosk-model-en-us-0.21")
rec = KaldiRecognizer(model, 16000)                 #audio is recorded at 16 kHz
rec.SetWords(True)
rec.SetPartialWords(True)

#------------Text to Speech
import pyttsx3
engine = pyttsx3.init()

# -----------LLMs
from langchain_community.embeddings import HuggingFaceBgeEmbeddings
from langchain_community.vectorstores import Chroma
from langchain.text_splitter import RecursiveCharacterTextSplitter
import pandas as pd
from langchain.docstore.document import Document

from llm_pipe import groq_pipeline, moondream_pipeline

import re #used to extract the file names

image_df = pd.read_csv("image-descriptions.csv")
lines = image_df.apply(lambda row: "Image Description: " + str(row["Description"]) + " File Name: " + str(row["File name"]), axis=1).tolist()

# convert list of strings to list of documents
text_splitter = RecursiveCharacterTextSplitter()
splits = text_splitter.create_documents(lines)
model_name = "BAAI/bge-small-en"
model_kwargs = {"device": "cpu"}
encode_kwargs = {"normalize_embeddings": True}
embeddings = HuggingFaceBgeEmbeddings(
    model_name=model_name, model_kwargs=model_kwargs, encode_kwargs=encode_kwargs
)
vectorstore = Chroma.from_documents(documents=splits, embedding=embeddings, collection_metadata={"hnsw:space": "cosine"})
retriever = vectorstore.as_retriever(
    search_type="mmr",
    search_kwargs={'k': 5, 'lambda_mult': 0.25} # get top 5 results
)

def format_docs(docs):
    return "\n\n".join(doc.page_content for doc in docs)

# UUIDs
#SERVICE_UUID = "19B10000-E8F2-537E-4F6C-D104768A1214"
PHOTO_UUID = "19B10005-E8F2-537E-4F6C-D104768A1214"
AUDIO_UUID = "19b10001-e8f2-537e-4f6c-d104768a1214"
COMMAND_UUID = "19B10006-E8F2-537E-4F6C-D104768A1214"

class BLEApp:
    def __init__(self, master):
        self.speakingStatus = False
        self.rag = True
         
        self.master = master
        self.master.title("BLE Device Selector")
        self.master.geometry("1000x800")
        
        self.connect_button = Button(master, text="Select BLE Device", command=self.show_device_selector)
        self.connect_button.pack(pady=20)

        # Create a frame to hold the images
        self.img_frame2 = tk.Frame(self.master)
        self.img_frame2.pack()

    def rag_chain(self, question):
        print("-------rag--------")
        self.speakingStatus = True
        retrieved_docs = retriever.invoke(question)
        context = format_docs(retrieved_docs)
        answer = groq_pipeline(question, context)
        print(context)
        file_names = re.findall(r'File Name: (\d+)', context)   # get top 5 images related to query

        #destroy the previous images in the frame
        for widget in self.img_frame2.winfo_children():
            widget.destroy()
        
        # display top 5 images
        for file_name in file_names:
            image = Image.open("./Pictures/" + str(file_name) + ".jpg")
            image = image.resize((int(240*0.75), int(320*0.75)))
            photo = ImageTk.PhotoImage(image)

            # Create a label to display the image
            label = tk.Label(self.img_frame2, image=photo)
            label.image = photo
            label.pack(side=tk.LEFT, padx=5, pady=5)
        
        # Text to speech
        engine.say(answer)
        engine.runAndWait()
        self.speakingStatus = False

    def show_device_selector(self):
        self.device_window = Toplevel(self.master)
        self.device_window.title("Select Device")
        
        self.device_listbox = Listbox(self.device_window)
        self.device_listbox.pack(pady=10)
        self.device_listbox.bind('<Double-1>', self.on_device_select)

        self.label = Label(self.device_window, text="Scanning for devices...")
        self.label.pack(pady=10)

        threading.Thread(target=self.scan_devices, daemon=True).start()

    def scan_devices(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        devices = loop.run_until_complete(self.scan_and_filter_devices())
        
        self.device_listbox.delete(0, END)
        for device in devices:
            self.device_listbox.insert(END, device) #display devices

        self.label.config(text="Double-click a device to connect")

    async def scan_and_filter_devices(self):
        devices = await BleakScanner.discover()
        filtered_devices = [device for device in devices if device.name == "OpenGlass"]
        return filtered_devices

    def on_device_select(self, event):
        selected_index = self.device_listbox.curselection()[0]
        selected_device = self.device_listbox.get(selected_index)
        selected_address = selected_device[:17]  # extract the device address

        self.device_window.destroy()

        threading.Thread(target=asyncio.run, args=(self.connect_to_device(selected_address),), daemon=True).start()

    async def connect_to_device(self, address):
        image_receiver = ImageReceiver()
        async with BleakClient(address, disconnected_callback=lambda _: image_receiver.reset()) as client:
            if client.is_connected:
                
                self.connect_button.destroy()
                self.connect_label = Label(self.master, text="Connected")
                self.connect_label.pack(pady=2)

                img_frame = tk.Frame(self.master)
                img_frame.pack()

                def img_notification_handler(sender, data):
                    encoded_image = image_receiver.feed(data)
                    if encoded_image is not None:
                        try:
                            image = decode_jpeg(encoded_image)
                        except InvalidImage:
                            print("Discarded invalid BLE image")
                            return
                        print("packet end")
                        delta = datetime.datetime.now() - datetime.datetime(1900, 1, 1)
                        img_name = int(delta.total_seconds())
                        image = image.rotate(-90, expand=True)

                        formatted_prompt = f"Thoroughly describe the image in detail. Include any objects and any text you see in the image in your answer"
                        description = moondream_pipeline(io.BytesIO(encoded_image), formatted_prompt)
                        image_df.loc[len(image_df)] = [str(img_name), str(description)]
                        vectorstore.add_documents([Document(page_content=("Image Description: " + str(description) + " File Name: " + str(img_name)))])
                        image.save("./Pictures/" + str(img_name) + ".jpg", "JPEG")

                        image = image.resize((int(240*1.5), int(320*1.5)))  # Resize image to fit in the grid cell
                        
                        photo = ImageTk.PhotoImage(image)
                        label = tk.Label(img_frame, image=photo)
                        label.image = photo
                        label.grid(row=0, column=1)

                # Subscribe to notifications
                await client.start_notify(PHOTO_UUID, img_notification_handler)
                print(f"Subscribed to {PHOTO_UUID}")
                
                def audio_notification_handler(sender, data):
                    if not self.speakingStatus:
                        if (rec.AcceptWaveform(bytes(data))):
                            handle_transcription(rec.Result())
                
                def handle_transcription(result):
                    parsed_data = json.loads(result)
                    print("transcription: " + parsed_data["text"])
                    if(parsed_data["text"] != ""):
                        threading.Thread(target=lambda:self.rag_chain(parsed_data["text"]), daemon=True).start()
                                
                await client.start_notify(AUDIO_UUID, audio_notification_handler)
                print(f"Subscribed to {AUDIO_UUID}")

                def command_notification_handler(sender, data): # I dont really need this actually
                    print(data)
                        
                await client.start_notify(COMMAND_UUID, command_notification_handler)
                print(f"Subscribed to {COMMAND_UUID}")

                # Keep the connection open to receive notifications
                while client.is_connected:
                    await asyncio.sleep(1)

if __name__ == "__main__":
    root = Tk()
    app = BLEApp(root)
    root.mainloop()
    image_df.to_csv("image-descriptions.csv", index=False)  # save image descriptions to csv file
