package demo.invoice;

/** Synthetic service: the rule and call are examples, not runtime evidence. */
public class InvoiceService {
    private InvoiceMapper invoiceMapper;

    public void openInvoice(double amount, double availableAmount) {
        if (amount <= 0 || amount > availableAmount) {
            throw new IllegalArgumentException("synthetic amount validation");
        }
        invoiceMapper.insert(amount);
    }
}
