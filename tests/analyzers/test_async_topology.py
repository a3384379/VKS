from test_java_spring import analyze, edges


def test_kafka_and_xxl_endpoints_are_exact_static_declarations_with_gaps():
    result = analyze(
        {
            "InvoiceEvents.java": """package demo;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.annotation.KafkaListener;
import com.xxl.job.core.handler.annotation.XxlJob;
public class InvoiceEvents {
  private KafkaTemplate<String,String> kafka;
  public void publish(String body) { kafka.send("invoices", body); }
  @KafkaListener(topics={"invoices", "failures"}, groupId="invoice-reader")
  public void receive(String body) { save(body); }
  @XxlJob("invoiceNightly") public void nightly() { save("nightly"); }
  public void save(String body) {}
}"""
        }
    )
    endpoints = [e for e in result.entities if e["metadata"].get("adapter") in ("kafka", "xxl_job")]
    assert len(endpoints) == 4
    assert {e["metadata"].get("topic") for e in endpoints if e["metadata"]["adapter"] == "kafka"} == {
        "invoices",
        "failures",
    }
    assert any(
        a["metadata"].get("adapter") == "xxl_job" and ".nightly(" in b["display_name"]
        for a, b in edges(result, "may_call")
    )
    assert any(
        ".publish(" in a["display_name"] and b["metadata"].get("direction") == "produce"
        for a, b in edges(result, "may_call")
    )
    assert any(
        a["metadata"].get("direction") == "consume" and ".receive(" in b["display_name"]
        for a, b in edges(result, "may_call")
    )
    assert all(r["causal_basis"] != "correlated_runtime" for r in result.relations)
    assert {"VKS_KAFKA_DEPLOYMENT_UNKNOWN", "VKS_XXL_SCHEDULE_UNKNOWN"} <= {i["code"] for i in result.issues}
    assert all(e["locator"]["startLine"] > 1 for e in endpoints)


def test_dynamic_topics_shadowed_types_nested_scopes_and_unrelated_annotations_do_not_invent_endpoints():
    result = analyze(
        {
            "Noise.java": """package demo;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.annotation.KafkaListener;
import com.xxl.job.core.handler.annotation.XxlJob;
public class Noise {
  private KafkaTemplate<String,String> kafka;
  public void dynamic(String topic) { kafka.send(topic, "body"); }
  public void shadow(Other kafka) { kafka.send("notKafka", "body"); }
  public void nested() { Runnable r=()->kafka.send("nested", "body"); }
  @KafkaListener(topicPattern="invoice.*") public void regex(String body) {}
  @KafkaListener(topics="${topic}") public void property(String body) {}
  @XxlJob("${handler}") public void job() {}
}
class Other { void send(String a,String b) {} }"""
        }
    )
    assert not [e for e in result.entities if e["metadata"].get("adapter")]
    assert {"VKS_KAFKA_DYNAMIC_TOPIC", "VKS_XXL_DYNAMIC_HANDLER"} <= {i["code"] for i in result.issues}
    result = analyze(
        {
            "Fake.java": """package demo;
import other.KafkaListener; import other.XxlJob;
public class Fake { @KafkaListener(topics="invoices") @XxlJob("job") public void fake() {} }"""
        }
    )
    assert not [e for e in result.entities if e["metadata"].get("adapter")]


def test_duplicate_job_handlers_are_ambiguous_and_topics_do_not_assume_shared_cluster():
    raw = """package demo; import com.xxl.job.core.handler.annotation.XxlJob;
public class Jobs { @XxlJob("same") public void one() {} @XxlJob("same") public void two() {} }"""
    result = analyze({"Jobs.java": raw})
    jobs = [e for e in result.entities if e["metadata"].get("adapter") == "xxl_job"]
    assert len(jobs) == 2 and all(e["metadata"]["resolution_state"] == "ambiguous" for e in jobs)
    assert "VKS_XXL_HANDLER_CONFLICT" in {i["code"] for i in result.issues}
    assert all(e["metadata"]["runtime_source_alignment"] == "unknown" for e in jobs)
