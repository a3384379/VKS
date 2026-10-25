package fixture.billing;
public interface InvoiceMapper {
    String find(String customer);
    int insert(double amount);
    int note(String comment);
    String dynamic(String table);
}
