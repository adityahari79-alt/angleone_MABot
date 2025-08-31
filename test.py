import streamlit as st
import subprocess

def check_smartapi():
    try:
        import smartapi
        st.write("smartapi imported successfully")
    except ModuleNotFoundError:
        st.error("smartapi module NOT found")

    result = subprocess.run(["pip", "list"], capture_output=True, text=True)
    st.text(result.stdout)

check_smartapi()
