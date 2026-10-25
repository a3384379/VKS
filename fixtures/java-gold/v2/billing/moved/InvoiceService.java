

// The unchanged method declarations moved in the source snapshot.
package fixture.billing;

public class InvoiceService {
    private InvoiceMapper mapper;

    public String find(String customer) {
        mapper.find(customer);
        return customer;
    }

    public void save(double amount) {
        mapper.insert(amount);
    }

    public void save(String comment) {
        mapper.note(comment);
    }

    public void ambiguous(Object value) {
        // Library-return type inference is deliberately outside this support profile.
        unknown(String.valueOf(value));
    }

    public void unknown(String value) { }
    public void unknown(Integer value) { }
}
