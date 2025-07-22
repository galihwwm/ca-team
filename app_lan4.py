import streamlit as st
import os
import uuid
from time import time
from concurrent.futures import ThreadPoolExecutor
from code_28 import (
    parse_pdf_to_markdown_with_images,
    load_or_create_index,
    load_cem_workunits,
    load_developer_actions,
    EvidenceAgent,
    PartAgent,
    WorkUnitRetriever,
    DeveloperActionRetriever,
    NavigatorAgent,
    EvaluationAgent,
    DeveloperAgent,
    UserAgent,
    ReportGenerator
)
from imports_and_helpers3_patched import sanitize_filename

def format_duration(seconds):
    if seconds < 60:
        return f"{seconds:.1f} sec"
    elif seconds < 3600:
        return f"{seconds//60:.0f} min {seconds%60:.0f} sec"
    else:
        return f"{seconds//3600:.0f} hr {seconds%3600//60:.0f} min"

@st.cache_resource
def load_standard_indexes():
    standard_indexes = {}
    for i, name in enumerate(["part1", "part2", "part3", "part4", "part5"], 1):
        std_path = f"d4dproject/standard/CC2022PART{i}R1.pdf"
        std_chunks, _ = parse_pdf_to_markdown_with_images(std_path)
        standard_indexes[name] = load_or_create_index(std_chunks, sanitize_filename(name))
    return standard_indexes

@st.cache_resource
def load_historical_index():
    hist_chunks = []
    for fname in os.listdir("d4dproject/historicalST"):
        if fname.lower().endswith(".pdf"):
            full_path = os.path.join("d4dproject/historicalST", fname)
            chunks, _ = parse_pdf_to_markdown_with_images(full_path)
            hist_chunks.extend(chunks)
    return load_or_create_index(hist_chunks, sanitize_filename("historical_index"))

@st.cache_resource
def load_workunit_database():
    return load_cem_workunits("d4dproject/standard/CEM2022R1.pdf")

@st.cache_resource
def load_developer_database():
    return load_developer_actions("d4dproject/standard/CC2022PART3R1.pdf")

# --- Token-based Routing ---
st.title("🔐 Login to Evaluation Assistant")
token = st.text_input("Enter your token")
if token:
    role = "Developer" if token.lower().startswith("dev") else "Evaluator"
    st.success(f"Logged in as {role}")

    # --- Load static resources once ---
    standard_indexes = load_standard_indexes()
    historical_index = load_historical_index()
    workunit_database = load_workunit_database()
    developer_database = load_developer_database()

    # --- Upload Document ---
    uploaded_file = st.file_uploader("Upload Security Target PDF", type=["pdf"])
    if uploaded_file:
        if "evidence_index" not in st.session_state or st.session_state.get("uploaded_name") != uploaded_file.name:
            path = f"uploads/{uuid.uuid4().hex}_{uploaded_file.name}"
            os.makedirs("uploads", exist_ok=True)
            with open(path, "wb") as f:
                f.write(uploaded_file.read())

            with st.spinner("Parsing and indexing Security Target..."):
                chunks, _ = parse_pdf_to_markdown_with_images(path)
                safe_evidence_name = sanitize_filename(f"evidence_{uuid.uuid4().hex[:6]}")
                evidence_index = load_or_create_index(chunks, safe_evidence_name)
                st.session_state["evidence_index"] = evidence_index
                st.session_state["uploaded_name"] = uploaded_file.name

            # Always recreate the agent after new upload
            evidence_agent = EvidenceAgent(evidence_index)
            part_agent = PartAgent(standard_indexes)
            workunit_retriever = WorkUnitRetriever(workunit_database)
            developer_retriever = DeveloperActionRetriever(developer_database)
            navigator = NavigatorAgent(role)
            evaluation_agent = EvaluationAgent(part_agent, historical_index, evidence_index)
            developer_agent = DeveloperAgent(part_agent, historical_index, evidence_index)
            user_agent = UserAgent(
                role, evidence_agent, part_agent,
                workunit_retriever, developer_retriever,
                navigator, evaluation_agent, developer_agent
            )
            st.session_state["user_agent"] = user_agent
        else:
            evidence_index = st.session_state["evidence_index"]
            user_agent = st.session_state["user_agent"]

        evaluation_agent = user_agent.evaluation_agent
        developer_agent = user_agent.developer_agent
        workunit_retriever = WorkUnitRetriever(workunit_database)
        developer_retriever = DeveloperActionRetriever(developer_database)

        # --- Role-based UI ---
        st.header(f"Welcome {role}")
        if st.button("💬 Chatbot"):
            st.session_state["mode"] = "chatbot"
        if st.button("📄 Recommendation Report" if role == "Developer" else "📄 Evaluation Report"):
            st.session_state["mode"] = "report"

        if st.session_state.get("mode") == "chatbot":
            question = st.text_area("Ask something...")
            if st.button("Ask"):
                with st.spinner("Generating response..."):
                    start_time = time()
                    result = user_agent.process_query(question)
                    elapsed = time() - start_time
                    if "single_eval_times" not in st.session_state:
                        st.session_state["single_eval_times"] = []
                    st.session_state["single_eval_times"].append(elapsed)
                    st.session_state["single_eval_times"] = st.session_state["single_eval_times"][-10:]
                    if not result:
                        st.warning("⚠️ No response returned from the system.")
                    else:
                        st.success(f"✅ Response generated in {elapsed:.2f} seconds")
                        for res in result:
                            st.subheader(res.get("workunit", res.get("developer_action", "Response")))
                            st.write(res.get("evaluation", res.get("guidance", res.get("response", ""))))

        if st.session_state.get("mode") == "report":
            st.markdown("### Select ASE Family")
            ase_family = st.selectbox("Choose ASE", [
                "ASE_INT.1", "ASE_CCL.1", "ASE_SPD.1", "ASE_OBJ.1", "ASE_OBJ.2",
                "ASE_ECD.1", "ASE_REQ.1", "ASE_REQ.2", "ASE_TSS.1", "ASE_TSS.2"
            ])

            if st.session_state.get("single_eval_times"):
                avg_query_time = sum(st.session_state["single_eval_times"]) / len(st.session_state["single_eval_times"])
            else:
                avg_query_time = 8
            st.info(f"⏳ Estimated time to complete: ~{format_duration(avg_query_time)}")

            if st.button("Generate"):
                prefix = ase_family.split(".")[0]
                try:
                    with st.spinner("Generating report..."):
                        start_time = time()
                        if role == "Evaluator":
                            workunits = workunit_retriever.retrieve_family(prefix)
                            if not workunits:
                                st.warning(f"No work units found for prefix {prefix}")
                            results = evaluation_agent.evaluate_cem(workunits)
                        else:
                            dev_actions = developer_retriever.retrieve_family(prefix)
                            if not dev_actions:
                                st.warning(f"No developer actions found for prefix {prefix}")
                            results = developer_agent.guide_development("", dev_actions)
                        elapsed = time() - start_time

                        if "single_eval_times" not in st.session_state:
                            st.session_state["single_eval_times"] = []
                        st.session_state["single_eval_times"].append(elapsed)
                        st.session_state["single_eval_times"] = st.session_state["single_eval_times"][-10:]

                        st.success(f"✅ Evaluation finished in {elapsed:.2f} seconds")

                        st.markdown("### 📄 Generated Report")
                        for res in results:
                            st.subheader(res.get("workunit", res.get("developer_action", "Result")))
                            st.write(res.get("evaluation", res.get("guidance", res.get("response", ""))))

                        report = ReportGenerator(prefix=f"{role.lower()}_report")
                        report.add_results(results, result_type="Evaluation" if role == "Evaluator" else "Developer")
                        path = report.save()
                        with open(path, "rb") as f:
                            st.download_button("📥 Download Report", f, file_name=os.path.basename(path))
                except Exception as e:
                    st.error(f"❌ Error during evaluation: {e}")
