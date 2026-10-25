package fixture.billing;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping(path = {"/pay", "/payments"}, headers = "X-Tenant", consumes = "application/json")
public class InvoiceController {
    private InvoiceService service;

    @GetMapping(path = "/records", params = {"state=paid", "!deleted"}, produces = "application/json")
    public String records(String customer) {
        return service.find(customer);
    }

    @PostMapping(path = {"/invoice", "/invoices"}, headers = "!X-Debug", consumes = "application/xml")
    public void submit(double amount) {
        service.save(amount);
    }

    @GetMapping(path = "/records", params = "state=pending")
    public String pending() {
        return service.find("pending");
    }

    @GetMapping("/reflect")
    public void reflective(Object target) throws Exception {
        target.getClass().getMethod("run").invoke(target);
    }
}
