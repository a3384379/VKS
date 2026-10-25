package demo.invoice;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** Synthetic Java 8 fixture. Never compiled or executed by VKS. */
@RestController
@RequestMapping("/pay/invoice")
public class InvoiceController {
    private InvoiceService invoiceService;

    @GetMapping("/records")
    public String queryRecords() {
        return "synthetic records only";
    }

    @PostMapping("/openInvoice")
    public void openInvoice(double amount, double availableAmount) {
        invoiceService.openInvoice(amount, availableAmount);
    }
}
