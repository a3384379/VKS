package fixture.billing;
import org.springframework.web.bind.annotation.*;
@RestController
@RequestMapping("/pay")
public class InvoiceController {
    @GetMapping(path="/records", params={"state=paid", "!deleted"}, headers="X-Tenant", produces="application/json", consumes="application/json")
    public String records(String customer) { return customer; }
}
