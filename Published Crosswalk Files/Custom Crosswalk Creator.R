###### This script allows users to create custom crosswalks for their needs.  Using the baseline crosswalk NAICS6_to_SIC4.dta
###### that bridges NAICS 6-digit codes to SIC 4-digit codes, or SIC4_to_NAICS6.dta that bridges SIC 4-digit codes to NAICS 6-digit
###### codes, users can aggregate up to whatever coarser bridge they desire.
###### Note that some custom crosswalks won't make a lot of sense.  Forcing NAICS 1-digit codes to split to the full set of
###### SIC 4-digit codes is less natural than, say, NAICS 5 to SIC 3, or NAICS 3 to SIC 2.  Use good judgement.


# Necessary packages
library(haven)
library(plyr)
library(dplyr)


#### NAICS to SIC

# Enter the path of the file NAICS6_to_SIC4.dta
NAICS6_to_SIC4 <- as.data.frame(read_dta("C:/Users/Zach S/OneDrive - Colostate/Research Papers/NAICS to SIC Crosswalks/Census Crosswalk Project/Published Crosswalk Files/NAICS6_to_SIC4.dta"))

# Here's the function.  It's just here for transparency, not to do anything with.
Build_a_Bridge <- function(a,b) {
  NewCW <- NAICS6_to_SIC4[ ,c(1,2,5:7)]
  NewCW$NAICS <- substr(NewCW$NAICS_Code, 1,a)
  NewCW$SIC <- substr(NewCW$SIC4, 1,b)
  NewCW <- ddply(NewCW, .(NAICS,SIC), numcolwise(sum)) %>%
    ddply(.(NAICS), mutate, Est_Sum = sum(Establishments), Emp_Sum = sum(Employees), Pay_Sum = sum(Annual_Payroll))
  NewCW$Est_weight <- round(NewCW$Establishments/NewCW$Est_Sum, 2)
  NewCW$Emp_weight <- round(NewCW$Employees/NewCW$Emp_Sum, 2)
  NewCW$Pay_weight <- round(NewCW$Annual_Payroll/NewCW$Pay_Sum, 2)
  CustomCW <- NewCW[ ,c(1:5,9:11)]
}

## Where it says "naics_level" replace with the number of digits you want your NAICS level to be, e.g., for a 3-digit code, put 3
## Where it says "sic_level" replace with the number of digits you want your SIC level to be, e.g., for a 2-digit code, put 2
MyCustomCrosswalk <- Build_a_Bridge(naics_level, sic_level)

# Export
#write.csv(MyCustomCrosswalk, "MyCustomCrosswalk.csv", row.names = F) # Uncomment and run this line when ready to export the crosswalk
#write_dta(MyCustomCrosswalk, "MyCustomCrosswalk.dta") # Or use this line if you prefer a .dta Stata file







#### SIC to NAICS

# Enter the path of the file SIC4_to_NAICS6.dta
SIC4_to_NAICS6 <- as.data.frame(read_dta("C:/Users/Zach S/OneDrive - Colostate/Research Papers/NAICS to SIC Crosswalks/Census Crosswalk Project/Published Crosswalk Files/SIC4_to_NAICS6.dta"))

# Here's the function.  It's just here for transparency, not to do anything with.
Build_a_Bridge2 <- function(a,b) {
  NewCW <- SIC4_to_NAICS6[ ,c(1,2,5:7)]
  NewCW$SIC <- substr(NewCW$SIC4, 1,a)
  NewCW$NAICS <- substr(NewCW$NAICS6, 1,b)
  NewCW <- ddply(NewCW, .(SIC,NAICS), numcolwise(sum)) %>%
    ddply(.(SIC), mutate, Est_Sum = sum(Establishments), Emp_Sum = sum(Employees), Pay_Sum = sum(Annual_Payroll))
  NewCW$Est_weight <- round(NewCW$Establishments/NewCW$Est_Sum, 2)
  NewCW$Emp_weight <- round(NewCW$Employees/NewCW$Emp_Sum, 2)
  NewCW$Pay_weight <- round(NewCW$Annual_Payroll/NewCW$Pay_Sum, 2)
  CustomCW <- NewCW[ ,c(1:5,9:11)]
}

## Where it says "sic_level" replace with the number of digits you want your SIC level to be, e.g., for a 2-digit code, put 2
## Where it says "naics_level" replace with the number of digits you want your NAICS level to be, e.g., for a 3-digit code, put 3
MyCustomCrosswalk2 <- Build_a_Bridge2(sic_level, naics_level)

# Export
#write.csv(MyCustomCrosswalk2, "MyCustomCrosswalk2.csv", row.names = F) # Uncomment and run this line when ready to export the crosswalk
#write_dta(MyCustomCrosswalk2, "MyCustomCrosswalk2.dta") # Or use this line if you prefer a .dta Stata file