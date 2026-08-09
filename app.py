from flask import Flask, render_template, request
from src.helper import download_hugging_face_embeddings
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_classic.chains import create_retrieval_chain
from langchain_classic.chains.combine_documents.stuff import create_stuff_documents_chain
from langchain_core.prompts import ChatPromptTemplate
from dotenv import load_dotenv
from src.prompt import *
import os

app = Flask(__name__)

load_dotenv()

PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if PINECONE_API_KEY:
    os.environ["PINECONE_API_KEY"] = PINECONE_API_KEY

if GEMINI_API_KEY:
    os.environ["GEMINI_API_KEY"] = GEMINI_API_KEY
    os.environ["GOOGLE_API_KEY"] = GEMINI_API_KEY
    os.environ["GENAI_API_KEY"] = GEMINI_API_KEY


embeddings = download_hugging_face_embeddings()

index_name = "medical-doctorai"

# Try to initialize Pinecone-backed vector store; continue without it if unavailable
retriever = None
docsearch = None
try:
    from langchain_pinecone import PineconeVectorStore

    docsearch = PineconeVectorStore.from_existing_index(
        index_name=index_name,
        embedding=embeddings
    )
    retriever = docsearch.as_retriever(search_type="similarity", search_kwargs={"k":3})
except Exception as e:
    print("Warning: langchain_pinecone or Pinecone index not available. Retriever disabled.", e)

# If langchain_pinecone isn't available, try the official Pinecone client
pinecone_client = None
pinecone_index = None
try:
    import pinecone
    from pinecone import Pinecone
    print('pinecone package file', pinecone.__file__)
    print('pinecone package version', getattr(pinecone, '__version__', 'unknown'))
    if PINECONE_API_KEY:
        pc = Pinecone(api_key=PINECONE_API_KEY)
        try:
            pinecone_index = pc.Index(name=index_name)
            pinecone_client = pc
            print(f"Using Pinecone index: {index_name}")
        except Exception as err:
            print(f"Pinecone index {index_name} not found via Pinecone client: {err}")
except Exception as e:
    print("Pinecone client not available:", e)

MODEL_CANDIDATES = [
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-flash-latest",
]


def create_llm(model_name: str):
    return ChatGoogleGenerativeAI(
        model=model_name,
        temperature=0.4,
        max_tokens=500,
    )


def normalize_response(resp):
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        return resp.get("answer") or resp.get("output") or resp.get("output_text") or resp.get("text") or str(resp)
    if hasattr(resp, "text"):
        return str(resp.text)
    if hasattr(resp, "generations"):
        generations = getattr(resp, "generations", [])
        if generations and isinstance(generations, list) and len(generations) > 0:
            first = generations[0]
            if first and isinstance(first, list) and len(first) > 0:
                return getattr(first[0], "text", None) or str(resp)
    if hasattr(resp, "get"):
        return resp.get("answer") or resp.get("output") or resp.get("output_text") or resp.get("text") or str(resp)
    return str(resp)


def should_retry_model_error(error: Exception) -> bool:
    msg = str(error).lower()
    return any(
        term in msg
        for term in ["503", "unavailable", "resource_exhausted", "high demand", "limit", "rate limit", "quota"]
    )


def invoke_llm(prompt_text: str) -> str:
    last_error = None
    for model_name in MODEL_CANDIDATES:
        try:
            print(f"LLM invoke using model: {model_name}")
            current_llm = create_llm(model_name)
            response = current_llm.invoke(prompt_text)
            return normalize_response(response)
        except Exception as err:
            last_error = err
            if should_retry_model_error(err):
                print(f"Model {model_name} unavailable, trying next fallback. Error: {err}")
                continue
            raise
    raise RuntimeError(f"All Gemini model candidates failed. Last error: {last_error}")


llm = create_llm(MODEL_CANDIDATES[0])
prompt = ChatPromptTemplate.from_messages(
    [
        ("system", system_prompt),
        ("human", "{input}"),
    ]
)

question_answer_chain = create_stuff_documents_chain(llm, prompt)
# Only build retrieval chain if a retriever is available
if retriever is not None:
    rag_chain = create_retrieval_chain(retriever, question_answer_chain)
else:
    rag_chain = None


@app.route("/")
def index():
    return render_template('chat.html')


@app.route("/get", methods=["GET", "POST"])
def chat():
    msg = request.form["msg"]
    input = msg
    print(input)
    if rag_chain is None:
        # If we have a Pinecone index, run retrieval + LLM prompt
        if pinecone_index is not None:
            try:
                qv = embeddings.embed_query(msg)
                res = pinecone_index.query(vector=qv, top_k=3, include_metadata=True)
                matches = getattr(res, 'matches', None) or res.get('matches', [])
                contexts = []
                for m in matches:
                    md = getattr(m, 'metadata', None) or (m.get('metadata', {}) if hasattr(m, 'get') else {})
                    if isinstance(md, dict):
                        text = md.get('text') or md.get('content') or md.get('page_content') or md.get('source') or md.get('body')
                    else:
                        text = None
                    if text:
                        contexts.append(text)
                context_block = "\n\n".join(contexts)
                if context_block:
                    filled_prompt = system_prompt.replace("{context}", context_block)
                    prompt_text = f"{filled_prompt}\n\nQuestion: {msg}"
                else:
                    prompt_text = system_prompt.replace("{context}", "") + f"\n\nQuestion: {msg}"

                answer = invoke_llm(prompt_text)
                return answer, 200
            except Exception as e:
                import traceback
                traceback.print_exc()
                return f"Error during Pinecone retrieval/LLM call: {e}", 500

        # Fallback: call the LLM directly so the app can answer without a vectorstore
        try:
            prompt_text = system_prompt.replace("{context}", "") + f"\n\nQuestion: {msg}"
            answer = invoke_llm(prompt_text)
            return answer, 200
        except Exception as e:
            import traceback
            traceback.print_exc()
            return f"Error calling LLM fallback: {e}", 500
    try:
        response = rag_chain.invoke({"input": msg})
        print("Response : ", response.get("answer"))
        return str(response.get("answer"))
    except Exception as e:
        # log the exception and return a safe message for the frontend
        import traceback
        traceback.print_exc()
        return f"Error: {e}", 500




if __name__ == '__main__':
    app.run(host="0.0.0.0", port= 8080, debug= True)